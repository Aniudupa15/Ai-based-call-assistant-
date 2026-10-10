// Minimal Unity-compatible shim so the same tests run on the PC without ESP-IDF.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern int unity_failures;
extern int unity_tests;
extern const char *unity_current;

#define UNITY_FAIL_(fmt, ...)                                                        \
    do {                                                                             \
        printf("FAIL %s (%s:%d): " fmt "\n", unity_current, __FILE__, __LINE__,      \
               __VA_ARGS__);                                                         \
        unity_failures++;                                                            \
        return;                                                                      \
    } while (0)

#define TEST_ASSERT_TRUE(c) do { if (!(c)) UNITY_FAIL_("%s", #c); } while (0)
#define TEST_ASSERT_FALSE(c) TEST_ASSERT_TRUE(!(c))
#define TEST_ASSERT_EQUAL(a, b) \
    do { long a_ = (long)(a), b_ = (long)(b); if (a_ != b_) UNITY_FAIL_("expected %ld, got %ld", a_, b_); } while (0)
#define TEST_ASSERT_EQUAL_UINT32(a, b) \
    do { uint32_t a_ = (a), b_ = (b); if (a_ != b_) UNITY_FAIL_("expected 0x%x, got 0x%x", (unsigned)a_, (unsigned)b_); } while (0)
#define TEST_ASSERT_EQUAL_STRING(a, b)                                               \
    do { const char *a_ = (a), *b_ = (b);                                            \
         if (!a_ || !b_ || strcmp(a_, b_)) UNITY_FAIL_("expected \"%s\", got \"%s\"", \
                                                       a_ ? a_ : "(null)", b_ ? b_ : "(null)"); } while (0)

#define RUN_TEST(fn)                                                                 \
    do { int before_ = unity_failures; unity_current = #fn; unity_tests++; fn();     \
         if (unity_failures == before_) printf("PASS %s\n", #fn); } while (0)

#define UNITY_BEGIN() (unity_failures = 0, unity_tests = 0)
#define UNITY_END()                                                                  \
    (printf("%d tests, %d failures\n", unity_tests, unity_failures), unity_failures)
