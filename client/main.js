const terminal = new Terminal({
    cursorBlink: true,
    convertEol: false,
    scrollback: 10000,
    fontFamily:
        '"SFMono-Regular", "Cascadia Code", "JetBrains Mono", Consolas, monospace',
    fontSize: 14,
    lineHeight: 1.25,
    cursorStyle: "bar",
    theme: {
        background: "#0b0d10",
        foreground: "#d8dce3",
        cursor: "#e7e9ed",
        cursorAccent: "#0b0d10",
        black: "#0b0d10",
        red: "#ff5f57",
        green: "#35d07f",
        yellow: "#e5b84b",
        blue: "#5c9cff",
        magenta: "#c678dd",
        cyan: "#56c8d8",
        white: "#d8dce3",
        brightBlack: "#555b65",
        brightRed: "#ff6b63",
        brightGreen: "#4be58f",
        brightYellow: "#f0ca63",
        brightBlue: "#72a9ff",
        brightMagenta: "#d998ed",
        brightCyan: "#6dd9e8",
        brightWhite: "#ffffff",
    },
});

terminal.open(
    document.getElementById("terminal"),
);

const WS_URL =
    "ws://127.0.0.1:8765/ws";

let socket = null;
let terminalMode = "command";
let terminalStatus = "initializing";
let commandBuffer = "";
let rawBuffer = "";
let rawTimer = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let pendingInput = [];

const startup =
    document.getElementById("startup");

const startupStatus =
    document.getElementById(
        "startup-status",
    );

const connectionDot =
    document.getElementById(
        "connection-dot",
    );

const connectionStatus =
    document.getElementById(
        "connection-status",
    );

const modeElement =
    document.getElementById("mode");

const statusMessage =
    document.getElementById(
        "status-message",
    );

function setConnectionState(state) {
    connectionDot.classList.remove(
        "connected",
        "disconnected",
    );

    if (state === "connected") {
        connectionDot.classList.add(
            "connected",
        );

        connectionStatus.textContent =
            "CONNECTED";

        if (terminalStatus === "ready") {
            statusMessage.textContent =
                "Remote shell ready";

            startupStatus.textContent =
                "Remote shell ready";
        } else if (
            terminalStatus === "closed"
        ) {
            statusMessage.textContent =
                "Remote shell closed";

            startupStatus.textContent =
                "Remote shell exited";
        } else {
            statusMessage.textContent =
                "Initializing remote shell";

            startupStatus.textContent =
                "Initializing remote shell";
        }

        return;
    }

    if (state === "disconnected") {
        connectionStatus.textContent =
            "DISCONNECTED";

        statusMessage.textContent =
            "Connection lost";

        startupStatus.textContent =
            "Waiting for connection";

        return;
    }

    connectionStatus.textContent =
        "CONNECTING";

    statusMessage.textContent =
        "Connecting...";

    startupStatus.textContent =
        "Establishing connection";
}

function updateTerminalStatus(status) {
    if (
        status !== "initializing" &&
        status !== "ready" &&
        status !== "closed"
    ) {
        return;
    }

    terminalStatus = status;

    if (status === "ready") {
        startup.classList.add("hidden");

        statusMessage.textContent =
            "Remote shell ready";

        startupStatus.textContent =
            "Remote shell ready";

        terminal.focus();

        return;
    }

    if (status === "closed") {
        startup.classList.remove(
            "hidden",
        );

        statusMessage.textContent =
            "Remote shell closed";

        startupStatus.textContent =
            "Remote shell exited";

        return;
    }

    startup.classList.remove(
        "hidden",
    );

    statusMessage.textContent =
        "Initializing remote shell";

    startupStatus.textContent =
        "Initializing remote shell";
}

function base64ToBytes(value) {
    const binary = atob(value);

    const bytes =
        new Uint8Array(
            binary.length,
        );

    for (
        let i = 0;
        i < binary.length;
        i++
    ) {
        bytes[i] =
            binary.charCodeAt(i);
    }

    return bytes;
}

function bytesToBase64(bytes) {
    let binary = "";

    for (const byte of bytes) {
        binary += String.fromCharCode(
            byte,
        );
    }

    return btoa(binary);
}

function stringToBase64(value) {
    return bytesToBase64(
        new TextEncoder().encode(
            value,
        ),
    );
}

function writeData(data) {
    const bytes =
        base64ToBytes(data);

    terminal.write(bytes);
    terminal.focus();
}

function sendSocketInput(data) {
    if (
        !socket ||
        socket.readyState !==
            WebSocket.OPEN
    ) {
        return false;
    }

    socket.send(
        JSON.stringify({
            type: "input",
            data: stringToBase64(data),
        }),
    );

    return true;
}

function queueInput(data) {
    if (sendSocketInput(data)) {
        return;
    }

    pendingInput.push(data);
}

function flushPendingInput() {
    if (
        !socket ||
        socket.readyState !==
            WebSocket.OPEN
    ) {
        return;
    }

    while (
        pendingInput.length > 0
    ) {
        const data =
            pendingInput.shift();

        if (!sendSocketInput(data)) {
            pendingInput.unshift(data);

            return;
        }
    }
}

function flushRawInput() {
    if (!rawBuffer) {
        rawTimer = null;

        return;
    }

    queueInput(rawBuffer);

    rawBuffer = "";
    rawTimer = null;
}

function scheduleRawFlush() {
    if (rawTimer !== null) {
        return;
    }

    rawTimer = setTimeout(
        flushRawInput,
        50,
    );
}

function eraseCommandCharacter() {
    if (!commandBuffer) {
        return;
    }

    commandBuffer =
        commandBuffer.slice(
            0,
            -1,
        );

    terminal.write(
        "\b \b",
    );
}

function handleCommandInput(data) {
    for (const char of data) {
        if (
            char === "\r" ||
            char === "\n"
        ) {
            terminal.write(
                "\b \b".repeat(
                    commandBuffer.length,
                ),
            );

            queueInput(
                commandBuffer + "\r",
            );

            commandBuffer = "";

            continue;
        }

        if (char === "\u007f") {
            eraseCommandCharacter();

            continue;
        }

        if (char === "\u0003") {
            commandBuffer = "";

            terminal.write(
                "^C\r\n",
            );

            queueInput(
                "\u0003",
            );

            continue;
        }

        if (char === "\u0004") {
            queueInput(
                "\u0004",
            );

            continue;
        }

        if (char === "\t") {
            queueInput(
                "\t",
            );

            continue;
        }

        commandBuffer += char;

        terminal.write(char);
    }
}

function handleRawInput(data) {
    rawBuffer += data;

    if (
        data.includes("\x1b") ||
        data.includes("\r") ||
        data.includes("\n") ||
        data.includes("\u0003") ||
        data.includes("\u0004")
    ) {
        flushRawInput();

        return;
    }

    scheduleRawFlush();
}

function handleInput(data) {
    if (
        terminalMode === "command"
    ) {
        handleCommandInput(data);

        return;
    }

    handleRawInput(data);
}

function updateMode(mode) {
    if (
        mode !== "command" &&
        mode !== "raw"
    ) {
        return;
    }

    if (
        terminalMode === "raw" &&
        rawBuffer
    ) {
        flushRawInput();
    }

    terminalMode = mode;
    commandBuffer = "";
    rawBuffer = "";

    if (rawTimer !== null) {
        clearTimeout(rawTimer);
        rawTimer = null;
    }

    modeElement.textContent =
        mode.toUpperCase();

    modeElement.classList.remove(
        "command",
        "raw",
    );

    modeElement.classList.add(
        mode,
    );

    terminal.focus();
}

function scheduleReconnect() {
    if (reconnectTimer !== null) {
        return;
    }

    reconnectTimer = setTimeout(
        () => {
            reconnectTimer = null;
            connectWebSocket();
        },
        reconnectDelay,
    );

    reconnectDelay = Math.min(
        reconnectDelay * 2,
        5000,
    );
}

function connectWebSocket() {
    if (
        socket &&
        (
            socket.readyState ===
                WebSocket.OPEN ||
            socket.readyState ===
                WebSocket.CONNECTING
        )
    ) {
        return;
    }

    setConnectionState(
        "connecting",
    );

    const ws = new WebSocket(
        WS_URL,
    );

    socket = ws;

    ws.onopen = () => {
        if (socket !== ws) {
            ws.close();

            return;
        }

        reconnectDelay = 1000;

        setConnectionState(
            "connected",
        );

        flushPendingInput();

        terminal.focus();
    };

    ws.onmessage = (event) => {
        if (socket !== ws) {
            return;
        }

        let data;

        try {
            data = JSON.parse(
                event.data,
            );
        } catch {
            return;
        }

        if (data.type === "status") {
            updateTerminalStatus(
                data.status,
            );

            return;
        }

        if (data.type === "data") {
            writeData(
                data.data,
            );

            return;
        }

        if (data.type === "mode") {
            updateMode(
                data.mode,
            );

            return;
        }
    };

    ws.onclose = () => {
        if (socket !== ws) {
            return;
        }

        socket = null;

        setConnectionState(
            "disconnected",
        );

        scheduleReconnect();
    };

    ws.onerror = () => {
        if (socket !== ws) {
            return;
        }

        ws.close();
    };
}

terminal.onData((data) => {
    handleInput(data);
});

document.addEventListener(
    "click",
    (event) => {
        if (
            event.target.closest(
                ".titlebar",
            ) ||
            event.target.closest(
                ".statusbar",
            )
        ) {
            return;
        }

        terminal.focus();
    },
);

window.addEventListener(
    "resize",
    () => {
        terminal.refresh(
            0,
            terminal.rows - 1,
        );
    },
);

setConnectionState(
    "connecting",
);

connectWebSocket();

terminal.focus();