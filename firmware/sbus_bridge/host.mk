# Host side: unit tests and the shared library the Python simulator loads.
CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
BUILD := build-host
LIB := $(BUILD)/libbridgecore.so

.PHONY: test lib clean
test: $(BUILD)/test_bridge_core
	./$(BUILD)/test_bridge_core
lib: $(LIB)

$(BUILD):
	mkdir -p $(BUILD)
$(BUILD)/test_bridge_core: test/test_bridge_core.c src/bridge_core.c src/bridge_core.h | $(BUILD)
	$(CC) $(CFLAGS) -fsanitize=address,undefined -o $@ test/test_bridge_core.c src/bridge_core.c
$(LIB): src/bridge_core.c src/bridge_core.h | $(BUILD)
	$(CC) $(CFLAGS) -shared -fPIC -o $@ src/bridge_core.c
clean:
	rm -rf $(BUILD)
