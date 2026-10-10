// Call state machine. Pure logic with no ESP-IDF dependencies, so it runs in
// host unit tests. The call task feeds it events and carries out the actions
// it returns (see app_main.c).
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CALL_ID_LEN 32
#define NUMBER_LEN 24

typedef enum {
    CALL_IDLE,       // module registered, link held open
    CALL_RINGING,    // waiting for the PC's decision, or for ATA to finish
    CALL_BOT,        // audio bridged to the PC
    CALL_VOICEMAIL,  // greeting from the card, recording to the card
    CALL_ENDING,     // hanging up and cleaning up
} call_state_t;

typedef enum {
    EV_INCOMING,          // RING with caller ID (number may be empty: withheld)
    EV_DECISION,          // decision from the PC
    EV_DECISION_TIMEOUT,  // no decision within DECISION_TIMEOUT_MS
    EV_ANSWER_OK,         // ATA succeeded
    EV_ANSWER_FAILED,     // ATA failed (caller already gone)
    EV_PC_HANGUP,         // hangup from the PC
    EV_NO_CARRIER,        // the caller hung up
    EV_LINK_DOWN,         // WebSocket down, or PC silent for 3 s during a bot call
    EV_TIME_LIMIT,        // bot-call backstop or voicemail maximum length
    EV_RING_STOPPED,      // no RING for a while on a call we left ringing
    EV_CLEANUP_DONE,      // the call task finished the Ending actions
} call_event_type_t;

typedef enum {
    DECISION_ANSWER,
    DECISION_REJECT,
    DECISION_IGNORE,
} decision_t;

typedef struct {
    call_event_type_t type;
    decision_t action;          // EV_DECISION
    uint32_t max_seconds;       // EV_DECISION
    char call_id[CALL_ID_LEN];  // EV_INCOMING (new id), EV_DECISION / EV_PC_HANGUP (to match)
    char number[NUMBER_LEN];    // EV_INCOMING
} call_event_t;

// Actions, returned as a bit mask. The call task performs them in bit order.
enum {
    ACT_SEND_CALL_START      = 1u << 0,
    ACT_START_DECISION_TIMER = 1u << 1,
    ACT_REJECT               = 1u << 2,   // AT+CHUP while ringing
    ACT_ANSWER               = 1u << 3,   // ATA; result comes back as EV_ANSWER_OK/FAILED
    ACT_START_BRIDGE         = 1u << 4,
    ACT_SEND_CALL_ANSWERED   = 1u << 5,
    ACT_START_CALL_TIMER     = 1u << 6,   // local backstop on bot-call length
    ACT_START_VOICEMAIL      = 1u << 7,
    ACT_STOP_BRIDGE          = 1u << 8,
    ACT_STOP_VOICEMAIL       = 1u << 9,
    ACT_PLAY_APOLOGY         = 1u << 10,
    ACT_HANGUP               = 1u << 11,
    ACT_SEND_CALL_END        = 1u << 12,
    ACT_QUEUE_VM_SMS         = 1u << 13,  // "Voicemail from <number> at <time>, <s>s"
    ACT_QUEUE_MISSED_SMS     = 1u << 14,  // card missing or full: text the number anyway
    ACT_CLEANUP              = 1u << 15,  // stop timers, then post EV_CLEANUP_DONE
};

typedef struct {
    call_state_t state;
    bool link_up;              // set by the call task from pc_link
    bool sd_ok;                // card mounted with enough free space
    bool awaiting_decision;    // call_start sent, no decision yet
    bool answering_voicemail;  // ATA was sent to take a voicemail
    bool left_ringing;         // rejected nothing, answered nothing: let it ring out
    bool call_start_sent;
    char call_id[CALL_ID_LEN];
    char number[NUMBER_LEN];
    const char *end_reason;    // caller_hangup | pc_hangup | link_lost | time_limit
    uint32_t max_seconds;
} call_fsm_t;

void call_fsm_init(call_fsm_t *fsm);
uint32_t call_fsm_handle(call_fsm_t *fsm, const call_event_t *ev);
const char *call_state_name(call_state_t state);

#ifdef __cplusplus
}
#endif
