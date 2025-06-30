#include <gpiod.h>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <chrono>
#include <csignal>
#include <cstdlib>

#define CHIP_NAME "gpiochip0" // Typical on Raspberry Pi
#define DIR_PIN 25
#define STEP_PIN 6
#define ENABLE_PIN 5

enum Direction { FORWARD, BACKWARD };

// Global for signal handler access
gpiod_chip* chip = nullptr;
gpiod_line* dir_line = nullptr;
gpiod_line* step_line = nullptr;
gpiod_line* enable_line = nullptr;

void pulseStepPin(gpiod_line* step_line, int steps) {
    for (int i = 0; i < steps; ++i) {
        gpiod_line_set_value(step_line, 1);
        std::this_thread::sleep_for(std::chrono::microseconds(20));
        gpiod_line_set_value(step_line, 0);
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
}

int main() {
    // Set up libgpiod chip and lines
    chip = gpiod_chip_open_by_name(CHIP_NAME);
    if (!chip) {
        std::cerr << "ERROR: Failed to open GPIO chip" << std::endl;
        return 1;
    }

    dir_line = gpiod_chip_get_line(chip, DIR_PIN);
    step_line = gpiod_chip_get_line(chip, STEP_PIN);
    enable_line = gpiod_chip_get_line(chip, ENABLE_PIN);

    if (!dir_line || !step_line || !enable_line) {
        std::cerr << "ERROR: Failed to access one or more GPIO lines" << std::endl;
        gpiod_chip_close(chip);
        return 1;
    }

    if (gpiod_line_request_output(dir_line, "stepper", 0) ||
        gpiod_line_request_output(step_line, "stepper", 0) ||
        gpiod_line_request_output(enable_line, "stepper", 1)) {
        std::cerr << "ERROR: Failed to request GPIO lines as outputs" << std::endl;
        gpiod_chip_close(chip);
        return 1;
    }

    std::string input;
    while (std::getline(std::cin, input)) {
        if (input.empty()) {
            gpiod_line_set_value(enable_line, 1); // disable motor
            continue;
        }

        std::istringstream input_stream(input);
        std::string direction_str;
        int steps;

        if (!(input_stream >> direction_str >> steps)) {
            std::cout << "ERROR: Invalid command format" << std::endl;
            std::cout << "DONE" << std::endl;
            std::cout.flush();
            continue;
        }

        Direction dir;
        if (direction_str == "FORWARD") dir = FORWARD;
        else if (direction_str == "BACKWARD") dir = BACKWARD;
        else {
            std::cout << "ERROR: Invalid direction" << std::endl;
            std::cout << "DONE" << std::endl;
            std::cout.flush();
            continue;
        }

        std::cout << "Got line: \"" << direction_str << "\"" << std::endl;
        std::cout.flush();

        gpiod_line_set_value(enable_line, 0);
        gpiod_line_set_value(dir_line, dir == FORWARD ? 1 : 0);
        std::this_thread::sleep_for(std::chrono::microseconds(100));
        pulseStepPin(step_line, steps);
        gpiod_line_set_value(enable_line, 1);

        std::cout << "SUCCESS: Moved " << steps << " steps in " << direction_str << " direction" << std::endl;
        std::cout << "DONE" << std::endl;
        std::cout.flush();
    }

    // Final cleanup
    gpiod_chip_close(chip);
    return 0;
}
