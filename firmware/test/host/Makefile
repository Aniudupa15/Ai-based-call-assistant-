# Runs the firmware's pure-logic tests on the PC: make -C firmware/test/host
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O1 -g
SRCS = main.c ../main/test_call_fsm.c ../../main/call_fsm.c

test: test_runner
	./test_runner

test_runner: $(SRCS) unity.h ../../main/call_fsm.h
	$(CC) $(CFLAGS) -I. -I../../main -o $@ $(SRCS)

clean:
	rm -f test_runner

.PHONY: test clean
