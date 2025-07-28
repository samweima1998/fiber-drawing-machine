# ---------- toolchain & flags -----------------------------------
CXX      := g++
CXXFLAGS := -std=c++20 -O2 -Wall -Wextra -pedantic
LDFLAGS  := -lgpiod -lpthread
# ----------------------------------------------------------------

# Target directory for the binaries ( ../build relative to src/)
BUILD_DIR := ../build

# All .cpp files in this directory
SRCS    := $(wildcard *.cpp)
# stepperMotor.cpp → stepperMotor
BASEN   := $(patsubst %.cpp,%,$(SRCS))
# ../build/stepperMotor  …
TARGETS := $(addprefix $(BUILD_DIR)/,$(BASEN))

# default goal ----------------------------------------------------
.PHONY: all
all: $(TARGETS)

# ensure the directory exists (one *and only one* rule for it!)
$(BUILD_DIR):
	@mkdir -p $@

# pattern rule: build each executable straight into BUILD_DIR -----
# $@ = ../build/foo     $< = foo.cpp
$(BUILD_DIR)/%: %.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

# housekeeping ----------------------------------------------------
.PHONY: clean debug rebuild

clean:
	$(RM) $(TARGETS)

debug:
	$(MAKE) CXXFLAGS='$(CXXFLAGS) -Og -g' all

rebuild: clean all
