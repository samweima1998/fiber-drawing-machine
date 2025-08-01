#include <gpiod.h>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <atomic>
#include <optional>
#include <fcntl.h>
#include <unistd.h>

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

// Add global atomic flag and thread handle
std::atomic<bool> continuous_running(false);
std::thread continuous_thread;

void pulseStepPin(gpiod_line* step_line, int steps) {
    for (int i = 0; i < steps; ++i) {
        gpiod_line_set_value(step_line, 1);
        std::this_thread::sleep_for(std::chrono::microseconds(20));
        gpiod_line_set_value(step_line, 0);
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
}

// Function to send continuous step pulses
void continuousStep(gpiod_line* step_line, int direction, int interval_us) {
    while (continuous_running) {
        gpiod_line_set_value(step_line, 1);
        std::this_thread::sleep_for(std::chrono::microseconds(20));
        gpiod_line_set_value(step_line, 0);
        std::this_thread::sleep_for(std::chrono::microseconds(interval_us));
    }
}

// --- Continuous pressure streaming: use a reader thread and atomic variable ---
#include <atomic>
#include <thread>

std::atomic<float> latest_pressure_value(0.0f);
std::atomic<bool> pressure_reader_running(false);
std::thread pressure_reader_thread;

void pressureReader() {
    std::string line;
    while (pressure_reader_running) {
        if (std::getline(std::cin, line)) {
            try {
                latest_pressure_value = std::stof(line);
                std::cout << "Received pressure: " << latest_pressure_value << std::endl;
                std::cout.flush();
            } catch (...) {
                // Ignore parse errors
            }
        } else {
            // Sleep briefly to avoid busy-waiting
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
}

void guardedMove(gpiod_line* step_line, gpiod_line* dir_line, gpiod_line* enable_line, Direction dir, int steps, int interval_us, float pressure_threshold) {
    // Start pressure reader thread
    pressure_reader_running = true;
    pressure_reader_thread = std::thread(pressureReader);

    gpiod_line_set_value(enable_line, 0);
    gpiod_line_set_value(dir_line, dir == FORWARD ? 1 : 0);
    std::this_thread::sleep_for(std::chrono::microseconds(100));

    int steps_taken = 0;
    while (steps_taken < steps) {
        float current_pressure = latest_pressure_value.load();
        if (current_pressure < pressure_threshold) {
            // Step
            gpiod_line_set_value(step_line, 1);
            std::this_thread::sleep_for(std::chrono::microseconds(20));
            gpiod_line_set_value(step_line, 0);
            std::this_thread::sleep_for(std::chrono::microseconds(interval_us));
            steps_taken++;
        } else {
            // Pressure too high, pause stepping
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    }
    gpiod_line_set_value(enable_line, 1);

    // Stop pressure reader thread
    pressure_reader_running = false;
    if (pressure_reader_thread.joinable()) pressure_reader_thread.join();
}

int main() {
    // std::cerr is defunct; using std::cout for all logs
    // Set up libgpiod chip and lines
    chip = gpiod_chip_open_by_name(CHIP_NAME);
    if (!chip) {
        std::cout << "ERROR: Failed to open GPIO chip" << std::endl;
        return 1;
    }

    dir_line = gpiod_chip_get_line(chip, DIR_PIN);
    step_line = gpiod_chip_get_line(chip, STEP_PIN);
    enable_line = gpiod_chip_get_line(chip, ENABLE_PIN);

    if (!dir_line || !step_line || !enable_line) {
        std::cout << "ERROR: Failed to access one or more GPIO lines" << std::endl;
        gpiod_chip_close(chip);
        return 1;
    }

    if (gpiod_line_request_output(dir_line, "stepper", 0) ||
        gpiod_line_request_output(step_line, "stepper", 0) ||
        gpiod_line_request_output(enable_line, "stepper", 1)) {
        std::cout << "ERROR: Failed to request GPIO lines as outputs" << std::endl;
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
        std::string command;
        input_stream >> command;

        if (command == "START") {
            std::string direction_str;
            int interval_us = 100; // default interval
            input_stream >> direction_str >> interval_us;

            Direction dir;
            if (direction_str == "FORWARD") dir = FORWARD;
            else if (direction_str == "BACKWARD") dir = BACKWARD;
            else {
                std::cout << "ERROR: Invalid direction" << std::endl;
                std::cout << "DONE" << std::endl;
                std::cout.flush();
                continue;
            }

            if (!continuous_running) {
                continuous_running = true;
                gpiod_line_set_value(enable_line, 0);
                gpiod_line_set_value(dir_line, dir == FORWARD ? 1 : 0);
                continuous_thread = std::thread(continuousStep, step_line, dir, interval_us);
                std::cout << "SUCCESS: Started continuous stepping in " << direction_str << std::endl;
            } else {
                std::cout << "ERROR: Continuous stepping already running" << std::endl;
            }
            std::cout << "DONE" << std::endl;
            std::cout.flush();
            continue;
        }

        if (command == "STOP") {
            if (continuous_running) {
                continuous_running = false;
                if (continuous_thread.joinable()) continuous_thread.join();
                gpiod_line_set_value(enable_line, 1);
                std::cout << "SUCCESS: Stopped continuous stepping" << std::endl;
            } else {
                std::cout << "ERROR: Continuous stepping not running" << std::endl;
            }
            std::cout << "DONE" << std::endl;
            std::cout.flush();
            continue;
        }

        if (command == "GUARDED_MOVE") {
            std::string direction_str;
            int steps, interval_us;
            float pressure_threshold;
            input_stream >> direction_str >> steps >> interval_us >> pressure_threshold;

            Direction dir;
            if (direction_str == "FORWARD") dir = FORWARD;
            else if (direction_str == "BACKWARD") dir = BACKWARD;
            else {
                std::cout << "ERROR: Invalid direction" << std::endl;
                std::cout << "DONE" << std::endl;
                std::cout.flush();
                continue;
            }

            guardedMove(step_line, dir_line, enable_line, dir, steps, interval_us, pressure_threshold);
            std::cout << "SUCCESS: Guarded move complete" << std::endl;
            std::cout << "DONE" << std::endl;
            std::cout.flush();
            continue;
        }

        // ...existing step command parsing...
        std::string direction_str = command;
        int steps;
        if (!(input_stream >> steps)) {
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
    continuous_running = false;
    if (continuous_thread.joinable()) continuous_thread.join();
    pressure_reader_running = false;
    if (pressure_reader_thread.joinable()) pressure_reader_thread.join();
    gpiod_chip_close(chip);
    return 0;
}
