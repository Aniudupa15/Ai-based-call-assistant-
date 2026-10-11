#include "call_fsm.h"

#include <string.h>

static void copy_str(char *dst, const char *src, size_t len)
{
    strncpy(dst, src, len - 1);
    dst[len - 1] = '\0';
}

static bool same_call(const call_fsm_t *fsm, const call_event_t *ev)
{
    // An empty id in the event means "the current call".
    return ev->call_id[0] == '\0' || strcmp(fsm->call_id, ev->call_id) == 0;
}

void call_fsm_init(call_fsm_t *fsm)
{
    bool link = fsm->link_up, sd = fsm->sd_ok;
    memset(fsm, 0, sizeof(*fsm));
    fsm->state = CALL_IDLE;
    fsm->link_up = link;
    fsm->sd_ok = sd;
}

static void reset_call(call_fsm_t *fsm)
{
    fsm->awaiting_decision = false;
    fsm->answering_voicemail = false;
    fsm->left_ringing = false;
    fsm->call_start_sent = false;
    fsm->end_reason = NULL;
    fsm->max_seconds = 0;
}

// No PC decision: take a voicemail if the card can hold one, otherwise let the
// call ring out and text the owner the number.
static uint32_t fall_back(call_fsm_t *fsm)
{
    fsm->awaiting_decision = false;
    if (fsm->sd_ok) {
        fsm->answering_voicemail = true;
        return ACT_ANSWER;
    }
    fsm->left_ringing = true;
    return ACT_QUEUE_MISSED_SMS;
}

static uint32_t end_ringing(call_fsm_t *fsm)
{
    // The caller gave up before we answered.
    uint32_t acts = ACT_CLEANUP;
    if (fsm->call_start_sent && !fsm->left_ringing) {
        fsm->end_reason = "caller_hangup";
        acts |= ACT_SEND_CALL_END;
    }
    fsm->state = CALL_ENDING;
    return acts;
}

static uint32_t end_bot_call(call_fsm_t *fsm, const char *reason, uint32_t extra)
{
    if (fsm->end_reason == NULL) {
        fsm->end_reason = reason;
    }
    fsm->state = CALL_ENDING;
    return ACT_STOP_BRIDGE | ACT_HANGUP | ACT_SEND_CALL_END | ACT_CLEANUP | extra;
}

static uint32_t end_voicemail(call_fsm_t *fsm)
{
    fsm->state = CALL_ENDING;
    return ACT_STOP_VOICEMAIL | ACT_HANGUP | ACT_QUEUE_VM_SMS | ACT_CLEANUP;
}

uint32_t call_fsm_handle(call_fsm_t *fsm, const call_event_t *ev)
{
    switch (fsm->state) {
    case CALL_IDLE:
        if (ev->type != EV_INCOMING) {
            return 0;
        }
        reset_call(fsm);
        copy_str(fsm->call_id, ev->call_id, sizeof(fsm->call_id));
        copy_str(fsm->number, ev->number, sizeof(fsm->number));
        fsm->state = CALL_RINGING;
        if (fsm->link_up) {
            fsm->awaiting_decision = true;
            fsm->call_start_sent = true;
            return ACT_SEND_CALL_START | ACT_START_DECISION_TIMER;
        }
        return fall_back(fsm);

    case CALL_RINGING:
        switch (ev->type) {
        case EV_DECISION:
            if (!fsm->awaiting_decision || !same_call(fsm, ev)) {
                return 0;  // late or stale: voicemail already chosen
            }
            fsm->awaiting_decision = false;
            if (ev->action == DECISION_ANSWER) {
                fsm->max_seconds = ev->max_seconds;
                return ACT_ANSWER;
            }
            if (ev->action == DECISION_REJECT) {
                fsm->state = CALL_ENDING;
                return ACT_REJECT | ACT_CLEANUP;
            }
            fsm->left_ringing = true;  // ignore: let it ring out
            return 0;
        case EV_DECISION_TIMEOUT:
            return fsm->awaiting_decision ? fall_back(fsm) : 0;
        case EV_LINK_DOWN:
            fsm->link_up = false;
            return fsm->awaiting_decision ? fall_back(fsm) : 0;
        case EV_ANSWER_OK:
            if (fsm->answering_voicemail) {
                fsm->state = CALL_VOICEMAIL;
                return ACT_START_VOICEMAIL;
            }
            fsm->state = CALL_BOT;
            return ACT_START_BRIDGE | ACT_SEND_CALL_ANSWERED | ACT_START_CALL_TIMER;
        case EV_ANSWER_FAILED:
        case EV_NO_CARRIER:
        case EV_RING_STOPPED:
            return end_ringing(fsm);
        default:
            return 0;  // repeated RING lines, stray hangups
        }

    case CALL_BOT:
        switch (ev->type) {
        case EV_PC_HANGUP:
            return same_call(fsm, ev) ? end_bot_call(fsm, "pc_hangup", 0) : 0;
        case EV_NO_CARRIER:
            return end_bot_call(fsm, "caller_hangup", 0);
        case EV_LINK_DOWN:
            fsm->link_up = false;
            return end_bot_call(fsm, "link_lost", ACT_PLAY_APOLOGY);
        case EV_TIME_LIMIT:
            return end_bot_call(fsm, "time_limit", 0);
        default:
            return 0;
        }

    case CALL_VOICEMAIL:
        switch (ev->type) {
        case EV_NO_CARRIER:
        case EV_TIME_LIMIT:
            return end_voicemail(fsm);
        case EV_LINK_DOWN:
            fsm->link_up = false;  // voicemail needs no link
            return 0;
        default:
            return 0;
        }

    case CALL_ENDING:
        if (ev->type == EV_CLEANUP_DONE) {
            fsm->state = CALL_IDLE;
            reset_call(fsm);
        } else if (ev->type == EV_LINK_DOWN) {
            fsm->link_up = false;
        }
        return 0;
    }
    return 0;
}

const char *call_state_name(call_state_t state)
{
    switch (state) {
    case CALL_IDLE: return "idle";
    case CALL_RINGING: return "ringing";
    case CALL_BOT: return "bot_call";
    case CALL_VOICEMAIL: return "voicemail";
    case CALL_ENDING: return "ending";
    }
    return "?";
}
