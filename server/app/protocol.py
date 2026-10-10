"""Constants shared with the firmware. docs/protocol.md is the source of truth."""

PROTOCOL_VERSION = "1.0"

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = 20

# ESP32 -> PC
HELLO = "hello"
CALL_START = "call_start"
CALL_ANSWERED = "call_answered"
CALL_END = "call_end"
HEALTH = "health"
SMS_RESULT = "sms_result"

# PC -> ESP32
TIME = "time"
DECISION = "decision"
CLEAR_AUDIO = "clear_audio"
HANGUP = "hangup"
SEND_SMS = "send_sms"
ERROR = "error"

END_REASONS = {"caller_hangup", "pc_hangup", "link_lost", "time_limit"}
ACTIONS = {"answer", "reject", "ignore"}

CLOSE_UNAUTHORISED = 4401
CLOSE_VERSION = 4409
CLOSE_REPLACED = 4000

SMS_MAX_CHARS = 160


def major(version: str) -> str:
    return str(version).split(".", 1)[0]
