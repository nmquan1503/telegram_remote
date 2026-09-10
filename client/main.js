const terminal = document.getElementById("terminal");
const input = document.getElementById("input");

const SERVER_URL = "http://127.0.0.1:8765";
const WS_URL = "ws://127.0.0.1:8765/ws";

let socket;
let outputLine = null;
let commandRunning = false;


function addLine(text, className = "output") {
    const line = document.createElement("div");

    line.className = `line ${className}`;
    line.textContent = text;

    terminal.insertBefore(line, terminal.lastElementChild);
    terminal.scrollTop = terminal.scrollHeight;

    return line;
}


function addCommand(command) {
    const line = document.createElement("div");
    line.className = "line";

    const prompt = document.createElement("span");
    prompt.className = "prompt";
    prompt.textContent = "$";

    const commandText = document.createElement("span");
    commandText.className = "command";
    commandText.textContent = ` ${command}`;

    line.appendChild(prompt);
    line.appendChild(commandText);

    terminal.insertBefore(line, terminal.lastElementChild);
    terminal.scrollTop = terminal.scrollHeight;
}


function connectWebSocket() {
    socket = new WebSocket(WS_URL);

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === "state") {
            addCommand(data.command);

            outputLine = addLine(data.log);
            commandRunning = true;
            return;
        }

        if (data.type === "log") {
            if (!outputLine) {
                outputLine = addLine("");
            }

            outputLine.textContent = data.log;
            terminal.scrollTop = terminal.scrollHeight;
            return;
        }

        if (data.type === "end") {
            if (!outputLine) {
                outputLine = addLine("");
            }

            outputLine.textContent = data.log;

            outputLine = null;
            commandRunning = false;

            terminal.scrollTop = terminal.scrollHeight;
        }
    };

    socket.onclose = () => {
        setTimeout(connectWebSocket, 1000);
    };

    socket.onerror = () => {
        socket.close();
    };
}


async function sendCommand(command) {
    const response = await fetch(`${SERVER_URL}/command`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({ command })
    });

    if (!response.ok) {
        throw new Error(`Server error: ${response.status}`);
    }

    const data = await response.json();

    if (!data.ok) {
        throw new Error(data.error || "Unknown error");
    }
}


async function executeCommand(command) {
    if (commandRunning) {
        addLine("A command is already running.", "error");
        return;
    }

    commandRunning = true;

    addCommand(command);

    input.value = "";

    try {
        await sendCommand(command);
    } catch (error) {
        commandRunning = false;

        addLine(
            `ERROR: ${error.message}`,
            "error"
        );
    }

    input.focus();
}


input.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") {
        return;
    }

    const command = input.value.trim();

    if (!command) {
        return;
    }

    await executeCommand(command);
});


document.addEventListener("click", () => {
    input.focus();
});


connectWebSocket();
input.focus();