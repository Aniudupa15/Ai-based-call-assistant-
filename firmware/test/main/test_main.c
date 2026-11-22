#include "unity.h"

void run_call_fsm_tests(void);

void app_main(void)
{
    UNITY_BEGIN();
    run_call_fsm_tests();
    UNITY_END();
}
