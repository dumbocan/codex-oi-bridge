"""Shared constants for guardrails and report schema."""

REQUIRED_REPORT_KEYS = (
    "task_id",
    "goal",
    "actions",
    "observations",
    "console_errors",
    "network_findings",
    "ui_findings",
    "result",
    "evidence_paths",
)

ALLOWED_RESULT_VALUES = {"success", "partial", "failed"}

# Observation/operation commands allowed for Open Interpreter (shell mode).
SHELL_ALLOWED_COMMAND_PREFIXES = (
    "cat",
    "curl",
    "date",
    "echo",
    "env",
    "find",
    "grep",
    "head",
    "hostname",
    "ifconfig",
    "ip",
    "ls",
    "netstat",
    "ping",
    "printenv",
    "ps",
    "pwd",
    "rg",
    "sed",
    "tail",
    "top",
    "uname",
    "uptime",
    "wc",
    "which",
    "whoami",
    "xwininfo",
    "xdotool",
    "wmctrl",
)

# GUI mode explicit allowlist.
GUI_ALLOWED_COMMAND_PREFIXES = tuple(
    sorted(
        {
            *SHELL_ALLOWED_COMMAND_PREFIXES,
            "import",
            "scrot",
        }
    )
)

BLOCKED_COMMAND_TOKENS = (
    "rm",
    "rmdir",
    "mv",
    "dd",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    "kill",
    "killall",
    "pkill",
    "chmod",
    "chown",
    "git",
    "pip",
    "pip3",
    "apt",
    "apt-get",
    "npm",
    "yarn",
    "pnpm",
    "docker",
    "kubectl",
    "tee",
    ">",
    ">>",
)

SENSITIVE_COMMAND_TOKENS = (
    "sudo",
    "ssh",
    "scp",
    "curl",
    "wget",
)

GUI_STATE_CHANGING_TOKENS = (
    "xdotool click",
    "xdotool key",
    "xdotool type",
)

CODE_EXTENSIONS = (
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".rs",
    ".cpp",
    ".c",
    ".h",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
)
