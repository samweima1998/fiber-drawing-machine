from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from asyncio import Queue, create_task
import asyncio
from pathlib import Path
import logging
import uvicorn
from typing import List
import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import subprocess
import json
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import signal
from fastapi import Response
import serial

logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change "*" to your frontend URL for better security
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Ensure POST is included
    allow_headers=["*"],
)

# # Command models
# class Command(BaseModel):
#     cs_pin: int
#     args: str

# class CommandBatch(BaseModel):
#     commands: List[Command]

class StepperCommand(BaseModel):
    command: str  # "START", "STOP", "MOVE"
    direction: Optional[str] = None  # "FORWARD" or "BACKWARD"
    steps: Optional[int] = None      # Only for MOVE
    interval_us: Optional[int] = None  # Only for START

class StepperCommandBatch(BaseModel):
    commands: List[StepperCommand]

# Persistent process variables
# command_queue: Optional[Queue] = None
stepper_queue: Optional[Queue] = None

shutdown_flag = False

# Path to the control executable
current_file_path = Path(__file__).resolve()
# control_executable = current_file_path.parent.parent / "build" / "control"
# control_executable = current_file_path.parent.parent / "build" / "patternControl"
stepper_executable = current_file_path.parent.parent / "build" / "stepperMotor"

class InputData(BaseModel):
    drawing_temperature: float
    curing_temperature: float
    drawing_pressure: float
    drawing_height: float
    curing_intensity: float
    curing_time: float



# Global lock to prevent overlapping /send_data executions
send_data_lock = asyncio.Lock()

# Global variable to store the latest temperature
latest_temperature: float = 0.0

# Global variable to store the latest pressure (temperature-adjusted)
latest_pressure = 0.0

# Global variable to store the latest status
latest_status: str = "Idle"

# Initialize serial connection (do this in startup event)
ser = None
async def sensor_task():
    global latest_temperature, latest_pressure, shutdown_flag, ser
    while not shutdown_flag:
        try:
            if ser and ser.in_waiting:
                line = ser.readline().decode().strip()
                # Check for special protocol lines
                if line.startswith("# TARE_OK"):
                    await serial_line_queue.put(line)
                # Parse sensor data lines as before
                parts = line.split(",")
                if len(parts) == 3:
                    temp, raw_pressure, adj_pressure = map(float, parts)
                    latest_temperature = temp
                    latest_pressure = adj_pressure
        except Exception as e:
            logging.error(f"Failed to read pressure/temperature: {e}")
        await asyncio.sleep(0.01)

@app.get("/status")
async def get_status():
    status = {
        "current_temperature": latest_temperature,
        "current_pressure": latest_pressure,
        "status": latest_status,
    }
    return status

@app.post("/send_data")
async def receive_data(data: InputData):
    global latest_status, latest_temperature, latest_pressure
    if send_data_lock.locked():
        raise HTTPException(status_code=429, detail="Previous /send_data still in progress")

    async with send_data_lock:
        try:
            # Wait until temperature condition is met
            latest_status = "Waiting for drawing temperature"
            while abs(latest_temperature - data.drawing_temperature) > 0.5:
                await asyncio.sleep(0.1)
            logging.info("Drawing temperature reached.")

            # --- Trigger tare on Arduino ---
            if ser and ser.is_open:
                ser.reset_input_buffer()  # Clear any old data
                ser.write(b"t\n")
                ser.flush()
                logging.info("Sent 't' command to Arduino for tare.")
                latest_status = "Taring"

                # Wait for "# TARE_OK" confirmation (timeout after 5 seconds)
                import time
                start_time = time.time()
                while True:
                    try:
                        # Wait for a line from the queue, with timeout
                        line = await asyncio.wait_for(serial_line_queue.get(), timeout=0.5)
                        logging.info(f"Arduino response: {line}")
                        if "# TARE_OK" in line:
                            logging.info("Tare confirmed by Arduino.")
                            break
                        if time.time() - start_time > 10:
                            latest_status = "Taring Failed"
                            raise TimeoutError("Timeout waiting for Arduino tare confirmation.")
                    except asyncio.TimeoutError:
                        if time.time() - start_time > 10:
                            latest_status = "Taring Failed"
                            raise TimeoutError("Timeout waiting for Arduino tare confirmation.")
                        continue
            
            # Start continuous stepping
            result_future_start = asyncio.get_running_loop().create_future()
            await stepper_queue.put({
                "command": "START",
                "direction": "FORWARD",
                "interval_us": 100,
                "result": result_future_start
            })
            await result_future_start
            latest_status = "Approaching contact"

            # Wait until pressure condition is met
            while latest_pressure > -100:
                await asyncio.sleep(0.1)
            logging.info("Contact detected.")
            

            # Stop continuous stepping
            result_future_stop = asyncio.get_running_loop().create_future()
            await stepper_queue.put({
                "command": "STOP",
                "result": result_future_stop
            })
            await result_future_stop
            latest_status = "Maintaining contact"

            #Pressure sensitive drawing
            result_future_guarded_move = asyncio.get_running_loop().create_future()
            await stepper_queue.put({
                "command": "GUARDED_MOVE",
                "direction": "BACKWARD",
                "steps": int(data.drawing_height * 6250), # Convert mm to steps
                "interval_us": 100,
                "pressure_threshold": data.drawing_pressure,
                "result": result_future_guarded_move
            })
            latest_status = "Drawing"
            await result_future_guarded_move
            logging.info("Drawing complete.")
            latest_status = "Drawing complete"

            # # Move drawing_height*6250 (conversion from mm to steps) steps backward
            # result_future_move = asyncio.get_running_loop().create_future()
            # await stepper_queue.put({
            #     "command": "MOVE",
            #     "direction": "BACKWARD",
            #     "steps": int(data.drawing_height * 6250),  # Convert mm to steps
            #     "result": result_future_move
            # })
            # latest_status = "Drawing"
            # await result_future_move
            # logging.info("Drawing complete.")
            # latest_status = "Drawing complete"

            # Wait until temperature condition is met
            latest_status = "Waiting for curing temperature"
            while abs(latest_temperature - data.curing_temperature) > 0.5:
                await asyncio.sleep(0.1)
            logging.info("curing temperature reached.")

            # Start curing
            if ser and ser.is_open:
                ser.reset_input_buffer()  # Clear any old data
                ser.write(f"start curing {data.curing_intensity}\n".encode())
                ser.flush()
                logging.info(f"Sent 'start curing {data.curing_intensity}' command to Arduino.")
                latest_status = "curing"
            await asyncio.sleep(data.curing_time)  # Wait for curing time
            if ser and ser.is_open:
                ser.reset_input_buffer()  # Clear any old data
                ser.write(b"stop curing\n")
                ser.flush()
                logging.info("Sent 'stop curing' command to Arduino.")
                latest_status = "Curing complete"

        except Exception as e:
            logging.error(f"Error in /send_data: {e}")
            return {"status": "error", "message": str(e)}
    

async def stepper_processor():
    global shutdown_flag
    process = None

    logging.info("Stepper processor starts.")
    try:
        while not shutdown_flag:
            process = await asyncio.create_subprocess_exec(
                "sudo", "-S", str(stepper_executable),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            if not process.stdin or not process.stdout:
                raise RuntimeError("Stepper subprocess pipes not initialized.")

            logging.info("Persistent stepper subprocess started.")

            while not shutdown_flag:
                try:
                    command = await asyncio.wait_for(stepper_queue.get(), timeout=1.0)
                    
                    if process.returncode is not None:
                        raise RuntimeError("Stepper subprocess terminated before executing command.")

                    if command["command"] == "START":
                        cmd_str = f"START {command['direction']} {command.get('interval_us', 100)}\n"
                    elif command["command"] == "STOP":
                        cmd_str = "STOP\n"
                    elif command["command"] == "MOVE":
                        cmd_str = f"{command['direction']} {command['steps']}\n"
                    elif command["command"] == "GUARDED_MOVE":
                        cmd_str = f"GUARDED_MOVE {command['direction']} {command['steps']} {command.get('interval_us', 100)} {command['pressure_threshold']}\n"
                        logging.info(f"Sent to stepper: {cmd_str.strip()}")
                        process.stdin.write(cmd_str.encode())
                        await process.stdin.drain()

                        # Protocol: respond to REQUEST_PRESSURE lines
                        while True:
                            line = await process.stdout.readline()
                            if not line:
                                break
                            decoded = line.decode().strip()
                            logging.info(f"Stepper subprocess output: {decoded}")
                            if decoded == "DONE":
                                break
                            if decoded == "REQUEST_PRESSURE":
                                # Send latest pressure as a line
                                process.stdin.write(f"{latest_pressure}\n".encode())
                                await process.stdin.drain()
                        if command.get("result"):
                            command["result"].set_result(True)
                        continue  # Prevent duplicate command send and wait below
                    else:
                        raise ValueError(f"Unknown stepper command: {command['command']}")

                    logging.info(f"Sent to stepper: {cmd_str.strip()}")
                    process.stdin.write(cmd_str.encode())
                    await process.stdin.drain()

                    # Wait for DONE
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            break
                        decoded = line.decode().strip()
                        logging.info(f"Stepper subprocess output: {decoded}")
                        if decoded == "DONE":
                            break

                    if command.get("result"):
                        command["result"].set_result(True)

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logging.error(f"Error processing stepper command: {e}")
                    break

            if process.returncode is not None:
                if process.returncode < 0:
                    sig = -process.returncode
                    logging.warning(f"Stepper subprocess terminated by signal {sig} ({signal.Signals(sig).name})")
                else:
                    logging.warning(f"Stepper subprocess exited with code {process.returncode}")

    finally:
        if process:
            logging.info("Shutting down stepper subprocess...")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logging.warning("Stepper subprocess did not terminate. Killing it.")
                process.kill()
                await process.wait()
        logging.info("Stepper processor fully shut down.")

@app.on_event("startup")
async def startup_event():
    global command_queue, stepper_queue, shutdown_flag, ser, serial_line_queue
    shutdown_flag = False
    # command_queue = Queue()
    stepper_queue = Queue()
    serial_line_queue = Queue()  # <-- create here, in the running loop!

    # Function to check if process is running
    def is_running(executable):
        return os.system(f"pgrep -f {executable} > /dev/null") == 0

    # Kill only if running
    if is_running(stepper_executable):
        os.system(f"sudo pkill -f {stepper_executable}")

    # Initialize serial connection for pressure sensor
    try:
        ser = serial.Serial('/dev/serial/by-id/usb-Arduino_LLC_Arduino_NANO_33_IoT_7CB63C1050304D48502E3120FF191434-if00', 115200, timeout=1)
        logging.info("Serial connection to pressure sensor established.")
    except Exception as e:
        logging.error(f"Failed to establish serial connection: {e}")

    # Reversing the order of the two create tasks below causes CS1 to become unresponsive for unknown reasons
    asyncio.create_task(stepper_processor())
    asyncio.create_task(sensor_task())    # Start sensor
    

    logging.info("Startup event complete. Command and stepper processors initialized.")

async def shutdown_event():
    global shutdown_flag
    shutdown_flag = True
    logging.info("Shutdown signal received. Cleaning up resources...")

    # proc1 = await asyncio.create_subprocess_shell(f"sudo pkill -f {control_executable}")
    # await proc1.wait()

    proc2 = await asyncio.create_subprocess_shell(f"sudo pkill -f {stepper_executable}")
    await proc2.wait()

    # Close serial connection
    if ser and ser.is_open:
        ser.close()
        logging.info("Serial connection closed.")

    logging.info("Subprocesses terminated successfully.")



# Define the path to the frontend folder correctly
svelte_frontend = current_file_path.parent.parent / "frontend" / "build"

# Serve the Svelte index.html for the root route
@app.get("/")
async def serve_svelte():
    return FileResponse(svelte_frontend / "index.html")

# @app.post("/stepper")
# async def control_stepper(batch: StepperCommandBatch):
#     results = []
#     for cmd in batch.commands:
#         result_future = asyncio.get_running_loop().create_future()
#         await stepper_queue.put({
#             "direction": str(cmd.direction),  # force string copy
#             "steps": int(cmd.steps),          # force int copy
#             "result": result_future
#         })
#         results.append(result_future)
#     await asyncio.gather(*results)
#     return {"status": True, "results": results}

# Serve the static files (Svelte app)
# Ensure these files are mounted last, otherwise POST requests may fail
app.mount("/", StaticFiles(directory=svelte_frontend, html=True), name="build")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7070, log_level="info", access_log=False)
