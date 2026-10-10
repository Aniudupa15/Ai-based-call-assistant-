// Unit tests for the call state machine. Built by the ESP-IDF test app
// (idf.py -C firmware/test flash monitor) and on the PC (make -C firmware/test/host).
#include <string.h>

#include "call_fsm.h"
#include "unity.h"

static call_fsm_t fsm;

static call_event_t ev(call_event_type_t type)
{
    call_event_t e;
    memset(&e, 0, sizeof(e));
    e.type = type;
    return e;
}

static uint32_t send(call_event_type_t type)
{
    call_event_t e = ev(type);
    return call_fsm_handle(&fsm, &e);
}

static uint32_t incoming(const char *number)
{
    call_event_t e = ev(EV_INCOMING);
    strcpy(e.call_id, "c-20261012-143205");
    strcpy(e.number, number);
    return call_fsm_handle(&fsm, &e);
}

static uint32_t decide(decision_t action)
{
    call_event_t e = ev(EV_DECISION);
    e.action = action;
    e.max_seconds = 180;
    strcpy(e.call_id, "c-20261012-143205");
    return call_fsm_handle(&fsm, &e);
}

static void fresh(bool link, bool sd)
{
    fsm.link_up = link;
    fsm.sd_ok = sd;
    call_fsm_init(&fsm);
}

static void test_bot_call_pc_hangs_up(void)
{
    fresh(true, true);
    TEST_ASSERT_EQUAL_UINT32(ACT_SEND_CALL_START | ACT_START_DECISION_TIMER, incoming("+919812345678"));
    TEST_ASSERT_EQUAL(CALL_RINGING, fsm.state);
    TEST_ASSERT_EQUAL_STRING("+919812345678", fsm.number);
    TEST_ASSERT_EQUAL_UINT32(0, incoming("+919812345678"));  // repeated RING
    TEST_ASSERT_EQUAL_UINT32(ACT_ANSWER, decide(DECISION_ANSWER));
    TEST_ASSERT_EQUAL_UINT32(180, fsm.max_seconds);
    TEST_ASSERT_EQUAL_UINT32(ACT_START_BRIDGE | ACT_SEND_CALL_ANSWERED | ACT_START_CALL_TIMER,
                             send(EV_ANSWER_OK));
    TEST_ASSERT_EQUAL(CALL_BOT, fsm.state);
    uint32_t acts = send(EV_PC_HANGUP);
    TEST_ASSERT_EQUAL_UINT32(ACT_STOP_BRIDGE | ACT_HANGUP | ACT_SEND_CALL_END | ACT_CLEANUP, acts);
    TEST_ASSERT_EQUAL_STRING("pc_hangup", fsm.end_reason);
    TEST_ASSERT_EQUAL(CALL_ENDING, fsm.state);
    send(EV_CLEANUP_DONE);
    TEST_ASSERT_EQUAL(CALL_IDLE, fsm.state);
}

static void test_caller_hangs_up_during_bot_call(void)
{
    fresh(true, true);
    incoming("+919812345678");
    decide(DECISION_ANSWER);
    send(EV_ANSWER_OK);
    TEST_ASSERT_TRUE(send(EV_NO_CARRIER) & ACT_SEND_CALL_END);
    TEST_ASSERT_EQUAL_STRING("caller_hangup", fsm.end_reason);
}

static void test_link_lost_plays_apology(void)
{
    fresh(true, true);
    incoming("+919812345678");
    decide(DECISION_ANSWER);
    send(EV_ANSWER_OK);
    uint32_t acts = send(EV_LINK_DOWN);
    TEST_ASSERT_TRUE(acts & ACT_PLAY_APOLOGY);
    TEST_ASSERT_TRUE(acts & ACT_HANGUP);
    TEST_ASSERT_EQUAL_STRING("link_lost", fsm.end_reason);
}

static void test_time_limit_backstop(void)
{
    fresh(true, true);
    incoming("");
    decide(DECISION_ANSWER);
    send(EV_ANSWER_OK);
    TEST_ASSERT_TRUE(send(EV_TIME_LIMIT) & ACT_HANGUP);
    TEST_ASSERT_EQUAL_STRING("time_limit", fsm.end_reason);
}

static void test_reject(void)
{
    fresh(true, true);
    incoming("+919822222222");
    TEST_ASSERT_EQUAL_UINT32(ACT_REJECT | ACT_CLEANUP, decide(DECISION_REJECT));
    send(EV_CLEANUP_DONE);
    TEST_ASSERT_EQUAL(CALL_IDLE, fsm.state);
}

static void test_ignore_rings_out(void)
{
    fresh(true, true);
    incoming("+919822222222");
    TEST_ASSERT_EQUAL_UINT32(0, decide(DECISION_IGNORE));
    TEST_ASSERT_EQUAL_UINT32(0, send(EV_DECISION_TIMEOUT));   // no voicemail either
    uint32_t acts = send(EV_RING_STOPPED);
    TEST_ASSERT_EQUAL_UINT32(ACT_CLEANUP, acts);              // no call_end for ignored calls
}

static void test_decision_timeout_takes_voicemail(void)
{
    fresh(true, true);
    incoming("+919812345678");
    TEST_ASSERT_EQUAL_UINT32(ACT_ANSWER, send(EV_DECISION_TIMEOUT));
    TEST_ASSERT_EQUAL_UINT32(0, decide(DECISION_ANSWER));     // too late
    TEST_ASSERT_EQUAL_UINT32(ACT_START_VOICEMAIL, send(EV_ANSWER_OK));
    TEST_ASSERT_EQUAL(CALL_VOICEMAIL, fsm.state);
    TEST_ASSERT_EQUAL_UINT32(0, send(EV_PC_HANGUP));
    uint32_t acts = send(EV_NO_CARRIER);
    TEST_ASSERT_EQUAL_UINT32(ACT_STOP_VOICEMAIL | ACT_HANGUP | ACT_QUEUE_VM_SMS | ACT_CLEANUP, acts);
}

static void test_no_link_goes_straight_to_voicemail(void)
{
    fresh(false, true);
    TEST_ASSERT_EQUAL_UINT32(ACT_ANSWER, incoming("+919812345678"));
    send(EV_ANSWER_OK);
    TEST_ASSERT_EQUAL(CALL_VOICEMAIL, fsm.state);
    TEST_ASSERT_TRUE(send(EV_TIME_LIMIT) & ACT_QUEUE_VM_SMS);
}

static void test_link_drops_while_waiting(void)
{
    fresh(true, true);
    incoming("+919812345678");
    TEST_ASSERT_EQUAL_UINT32(ACT_ANSWER, send(EV_LINK_DOWN));
    TEST_ASSERT_TRUE(fsm.answering_voicemail);
}

static void test_no_card_texts_number(void)
{
    fresh(false, false);
    TEST_ASSERT_EQUAL_UINT32(ACT_QUEUE_MISSED_SMS, incoming("+919812345678"));
    TEST_ASSERT_EQUAL(CALL_RINGING, fsm.state);
    TEST_ASSERT_EQUAL_UINT32(ACT_CLEANUP, send(EV_NO_CARRIER));
}

static void test_caller_gives_up_while_pc_decides(void)
{
    fresh(true, true);
    incoming("+919812345678");
    uint32_t acts = send(EV_NO_CARRIER);
    TEST_ASSERT_EQUAL_UINT32(ACT_SEND_CALL_END | ACT_CLEANUP, acts);
    TEST_ASSERT_EQUAL_STRING("caller_hangup", fsm.end_reason);
}

static void test_answer_failed(void)
{
    fresh(true, true);
    incoming("+919812345678");
    decide(DECISION_ANSWER);
    TEST_ASSERT_TRUE(send(EV_ANSWER_FAILED) & ACT_CLEANUP);
    TEST_ASSERT_EQUAL(CALL_ENDING, fsm.state);
}

static void test_stale_hangup_ignored(void)
{
    fresh(true, true);
    incoming("+919812345678");
    decide(DECISION_ANSWER);
    send(EV_ANSWER_OK);
    call_event_t e = ev(EV_PC_HANGUP);
    strcpy(e.call_id, "c-old");
    TEST_ASSERT_EQUAL_UINT32(0, call_fsm_handle(&fsm, &e));
    TEST_ASSERT_EQUAL(CALL_BOT, fsm.state);
}

static void test_incoming_ignored_while_busy(void)
{
    fresh(true, true);
    incoming("+919812345678");
    decide(DECISION_ANSWER);
    send(EV_ANSWER_OK);
    TEST_ASSERT_EQUAL_UINT32(0, incoming("+919800000001"));
    TEST_ASSERT_EQUAL_STRING("+919812345678", fsm.number);
}

void run_call_fsm_tests(void)
{
    RUN_TEST(test_bot_call_pc_hangs_up);
    RUN_TEST(test_caller_hangs_up_during_bot_call);
    RUN_TEST(test_link_lost_plays_apology);
    RUN_TEST(test_time_limit_backstop);
    RUN_TEST(test_reject);
    RUN_TEST(test_ignore_rings_out);
    RUN_TEST(test_decision_timeout_takes_voicemail);
    RUN_TEST(test_no_link_goes_straight_to_voicemail);
    RUN_TEST(test_link_drops_while_waiting);
    RUN_TEST(test_no_card_texts_number);
    RUN_TEST(test_caller_gives_up_while_pc_decides);
    RUN_TEST(test_answer_failed);
    RUN_TEST(test_stale_hangup_ignored);
    RUN_TEST(test_incoming_ignored_while_busy);
}
