/*
 * darktable for TOS - browser front end.
 *
 * Connects noVNC to the WebSocket endpoint the launcher serves, and manages
 * the three states the user can actually find themselves in: waiting for the
 * desktop, using it, and having lost it. The last one is the one worth being
 * careful about, because a remote desktop drops for ordinary reasons - a
 * closed laptop lid, a sleeping access point, a rebooted NAS - and the page
 * has to be able to say which of those happened and offer a way forward
 * rather than leaving a frozen canvas.
 *
 * noVNC is vendored under ./vendor/novnc and is not modified.
 */

import RFB from './vendor/novnc/core/rfb.js';

/* ------------------------------------------------------------------ paths */

/*
 * Everything is derived from the page's own URL so the interface works both
 * at the root of its own port - where TOS opens the application in a new tab -
 * and under the platform's nginx prefix. The platform uses a secondary path,
 * which is why nothing here may start with a bare '/'.
 */
const BASE = (() => {
    const path = window.location.pathname;
    return path.endsWith('/') ? path : path.slice(0, path.lastIndexOf('/') + 1);
})();

const WEBSOCKET_PATH = 'websocket';
const RESTART_ENDPOINT = BASE + 'api/restart';

/* ------------------------------------------------------------------- ui */

const screenElement = document.getElementById('screen');
const overlay = document.getElementById('overlay');
const overlayTitle = document.getElementById('overlay-title');
const overlayMessage = document.getElementById('overlay-message');
const overlayHint = document.getElementById('overlay-hint');
const overlayActions = document.getElementById('overlay-actions');
const overlaySpinner = overlay.querySelector('.spinner');

const fitButton = document.getElementById('fit');
const fullscreenButton = document.getElementById('fullscreen');
const restartButton = document.getElementById('restart');

/** Show the full-page message. Pass `spinner: false` for a settled state. */
function showOverlay({ title, message, hint = '', spinner = false, isError = false, actions = false }) {
    overlayTitle.textContent = title;
    overlayMessage.textContent = message;
    overlayHint.textContent = hint;
    overlayHint.hidden = !hint;
    overlaySpinner.hidden = !spinner;
    overlayActions.hidden = !actions;
    overlay.classList.toggle('error', isError);
    overlay.hidden = false;
}

function hideOverlay() {
    overlay.hidden = true;
}

/* --------------------------------------------------------------- session */

let rfb = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let userClosed = false;
let connected = false;

/*
 * Backoff for reconnection. The first retries are quick because the common
 * cause is the desktop still starting up, which resolves in a second or two.
 * Later ones stretch out so that a NAS which is rebooting does not get a new
 * connection attempt every second for the minutes it takes to come back.
 */
const RECONNECT_DELAYS = [1000, 2000, 3000, 5000, 8000, 13000, 21000];

function nextReconnectDelay() {
    const index = Math.min(reconnectAttempts, RECONNECT_DELAYS.length - 1);
    return RECONNECT_DELAYS[index];
}

function websocketUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${window.location.host}${BASE}${WEBSOCKET_PATH}`;
}

function clearReconnect() {
    if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function scheduleReconnect(reason) {
    clearReconnect();
    const delay = nextReconnectDelay();
    reconnectAttempts += 1;

    const seconds = Math.round(delay / 1000);
    showOverlay({
        title: 'Reconnecting',
        message: reason,
        hint: seconds <= 1 ? 'Trying again now…' : `Trying again in ${seconds} seconds.`,
        spinner: true,
        actions: true,
    });

    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

function connect() {
    if (userClosed) {
        return;
    }

    // Detach the previous object before replacing it. noVNC holds listeners on
    // the element it was given, and leaving one attached while a second
    // connects produces two canvases fighting over the same container.
    if (rfb !== null) {
        try {
            rfb.disconnect();
        } catch (error) {
            /* Already down; nothing to do. */
        }
        rfb = null;
    }

    connected = false;
    updateControls();

    if (reconnectAttempts === 0) {
        showOverlay({
            title: 'Connecting',
            message: 'Opening the desktop…',
            spinner: true,
        });
    }

    try {
        rfb = new RFB(screenElement, websocketUrl());
    } catch (error) {
        scheduleReconnect(`The connection could not be opened: ${error.message}`);
        return;
    }

    rfb.addEventListener('connect', () => {
        connected = true;
        reconnectAttempts = 0;
        hideOverlay();
        updateControls();
    });

    rfb.addEventListener('disconnect', (event) => {
        connected = false;
        updateControls();
        const detail = event.detail || {};

        // A clean close is the server saying goodbye on purpose, which happens
        // during a restart of the service. Anything else is a dropped
        // connection, and the two deserve different words.
        if (detail.clean) {
            scheduleReconnect('The desktop session ended.');
        } else if (detail.code === 1006) {
            // No close frame at all. The usual cause is that the desktop was
            // not up yet, so the launcher answered the handshake with a 503
            // rather than switching protocols.
            scheduleReconnect('The desktop is not ready yet.');
        } else {
            scheduleReconnect('The connection to the desktop was lost.');
        }
    });

    rfb.addEventListener('securityfailure', (event) => {
        // Reached when the password was rejected after the browser had already
        // accepted it, which happens after an operator rotates access.txt.
        showOverlay({
            title: 'Authentication failed',
            message: event.detail && event.detail.reason
                ? event.detail.reason
                : 'The access password was rejected.',
            hint: 'Reload the page and enter the password from data/access.txt again.',
            isError: true,
            actions: true,
        });
    });

    // The desktop is a fixed-size virtual screen, so fitting it to the window
    // is the sensible default: scrolling a desktop is worse than scaling it.
    rfb.scaleViewport = true;
    rfb.clipViewport = false;
    rfb.background = '#0b0f18';
}

/* -------------------------------------------------------------- controls */

function updateControls() {
    const disabled = !connected;
    fitButton.disabled = disabled;
    fullscreenButton.disabled = disabled;
    restartButton.disabled = false; // Usable precisely when things are broken.
    fitButton.setAttribute('aria-pressed', String(rfb !== null && rfb.scaleViewport));
}

fitButton.addEventListener('click', () => {
    if (rfb === null) {
        return;
    }
    rfb.scaleViewport = !rfb.scaleViewport;
    fitButton.setAttribute('aria-pressed', String(rfb.scaleViewport));
});

fullscreenButton.addEventListener('click', () => {
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        document.documentElement.requestFullscreen().catch(() => {
            /* Refused, usually because the gesture was not recognised. Harmless. */
        });
    }
});

restartButton.addEventListener('click', async () => {
    restartButton.disabled = true;
    restartButton.textContent = 'Restarting…';
    try {
        const response = await fetch(RESTART_ENDPOINT, {
            method: 'POST',
            credentials: 'include',
            headers: { Accept: 'application/json' },
        });
        if (!response.ok) {
            throw new Error(`the server answered ${response.status}`);
        }
        // Give the launcher a moment to bring the editor back before the
        // WebSocket tries to attach; without the pause the retry fires while
        // the VNC server is mid-restart and fails for no useful reason.
        reconnectAttempts = 0;
        window.setTimeout(() => connect(), 1500);
    } catch (error) {
        showOverlay({
            title: 'The session could not be restarted',
            message: String(error.message || error),
            hint: 'Restart the application from the TOS App Center instead.',
            isError: true,
            actions: true,
        });
    } finally {
        restartButton.disabled = false;
        restartButton.textContent = 'Restart session';
    }
});

document.getElementById('action-retry').addEventListener('click', () => {
    reconnectAttempts = 0;
    connect();
});

document.getElementById('action-reload').addEventListener('click', () => {
    window.location.reload();
});

/* ---------------------------------------------------------------- startup */

window.addEventListener('beforeunload', () => {
    userClosed = true;
    clearReconnect();
    if (rfb !== null) {
        try {
            rfb.disconnect();
        } catch (error) {
            /* Closing anyway. */
        }
    }
});

// Coming back to a tab that was in the background is the single most common
// moment to find a dead connection: browsers throttle timers in hidden tabs,
// so the reconnect that should have run may never have fired.
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !connected && reconnectTimer === null && !userClosed) {
        connect();
    }
});

connect();
