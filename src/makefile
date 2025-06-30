# ------------ configurable bits ---------------------------------
CXX      := g++
CXXFLAGS := -std=c++20 -O2 -Wall -Wextra -pedantic   # adjust to taste
LDFLAGS  :=                                          # e.g. -pthread
# ----------------------------------------------------------------

# Where to drop the executables
BUILD_DIR := ../build           # relative to this Makefile

# Collect sources and derive target paths
SRCS      := $(wildcard *.cpp)
BINS      := $(basename $(SRCS))                     # foo.cpp → foo
PROGS     := $(addprefix $(BUILD_DIR)/,$(BINS))      # … → ../build/foo

# Default goal ---------------------------------------------------
.PHONY: all
all: $(BUILD_DIR) $(PROGS)

# Ensure the build directory exists
$(BUILD_DIR):
	@mkdir -p $@

# Pattern rule: compile each source into its exec in BUILD_DIR ----
# $@ = ../build/foo   $< = foo.cpp
$(BUILD_DIR)/%: %.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

# Convenience extras ---------------------------------------------
.PHONY: clean rebuild debug

clean:                  # remove everything in BUILD_DIR that we made
	$(RM) $(PROGS)

debug:                  # same build, but with -Og -g for debugging
	$(MAKE) CXXFLAGS='$(CXXFLAGS) -Og -g' all

rebuild: clean all      # nuke & re-build from scratch
