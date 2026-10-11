#include "unity.h"

int unity_failures;
int unity_tests;
const char *unity_current;

void run_call_fsm_tests(void);

int main(void)
{
    UNITY_BEGIN();
    run_call_fsm_tests();
    return UNITY_END() ? 1 : 0;
}
