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
    direction: str  # e.g., "left" or "right"
    steps: int

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

class DotData(BaseModel):
    index: int
    number: int

class DotBatch(BaseModel):
    dots: List[DotData]

# Global lock to prevent overlapping /send_dots executions
send_dots_lock = asyncio.Lock()

# Global variable to store the latest temperature
latest_temperature: float = 0.0

# Global variable to store the latest pressure
latest_pressure = 0.0

# Initialize serial connection (do this in startup event)
ser = None
async def pressure_reader_task():
    global latest_pressure, shutdown_flag, ser
    while not shutdown_flag:
        try:
            if ser and ser.in_waiting:
                line = ser.readline().decode().strip()
                # Expecting a float value from Arduino, e.g. "1.23"
                latest_pressure = float(line)
        except Exception as e:
            logging.error(f"Failed to read pressure: {e}")
        await asyncio.sleep(0.2)  # Adjust as needed

async def temperature_reader_task(sensor_path: str = "/sys/bus/w1/devices/28-00000fc8aa09/w1_slave"):
    global latest_temperature, shutdown_flag
    while not shutdown_flag:
        temp = read_temp(sensor_path)
        if temp is not None:
            latest_temperature = temp
        await asyncio.sleep(1)  # Read every second

def read_temp(sensor_path: str = "/sys/bus/w1/devices/28-00000fc8aa09/w1_slave") -> float:
    """
    Reads the temperature from a 1-wire sensor file.
    Returns the temperature in Celsius, or None if reading fails.
    """
    try:
        with open(sensor_path, "r") as f:
            lines = f.readlines()
        if len(lines) < 2 or "YES" not in lines[0]:
            return None
        temp_str = lines[1].split("t=")[-1].strip()
        return float(temp_str) / 1000.0
    except Exception as e:
        logging.error(f"Failed to read temperature: {e}")
        return None

@app.get("/status")
async def get_status():
    status = {
        "current_temperature": latest_temperature,
        "current_pressure": 1.23,
        "status": "Idle"
    }
    return status

@app.post("/send_data")
async def receive_dots(batch: DotBatch):
    if send_dots_lock.locked():
        raise HTTPException(status_code=429, detail="Previous /send_dots still in progress")

    async with send_dots_lock:
        try:
            # Stepper move BACKWARD
            result_future1 = asyncio.get_running_loop().create_future()
            await stepper_queue.put({
                "direction": "BACKWARD"[:],
                "steps": int(20000),
                "result": result_future1
            })
            await result_future1
            logging.info("Stepper BACKWARD complete.")
           
            # Stepper move FORWARD
            result_future2 = asyncio.get_running_loop().create_future()
            await stepper_queue.put({
                "direction": "FORWARD"[:],
                "steps": int(20000),
                "result": result_future2
            })
            await result_future2
            logging.info("Stepper FORWARD complete.")

        except Exception as e:
            logging.error(f"Error in /send_dots: {e}")
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

                    command_input = f"{command['direction']} {command['steps']}\n"
                    logging.info(f"Sent to stepper: {command_input.strip()}")

                    process.stdin.write(command_input.encode())
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
    global command_queue, stepper_queue, shutdown_flag, ser
    shutdown_flag = False
    # command_queue = Queue()
    stepper_queue = Queue()

    # Function to check if process is running
    def is_running(executable):
        return os.system(f"pgrep -f {executable} > /dev/null") == 0

    # Kill only if running
    # if is_running(control_executable):
    #     os.system(f"sudo pkill -f {control_executable}")

    if is_running(stepper_executable):
        os.system(f"sudo pkill -f {stepper_executable}")

    # Initialize serial connection for pressure sensor
    try:
        ser = serial.Serial('/dev/ttyUSB0', 9600, timeout=1)
        logging.info("Serial connection to pressure sensor established.")
    except Exception as e:
        logging.error(f"Failed to establish serial connection: {e}")

    # Reversing the order of the two create tasks below causes CS1 to become unresponsive for unknown reasons
    asyncio.create_task(stepper_processor())
    asyncio.create_task(temperature_reader_task())  # Start temperature reader
    asyncio.create_task(pressure_reader_task())    # Start pressure reader
    

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

# @app.post("/execute_batch")
# async def execute_batch_commands(batch: CommandBatch):
#     results = []
#     for cmd in batch.commands:
#         result_future = asyncio.get_running_loop().create_future()
#         await command_queue.put({"cs_pin": cmd.cs_pin, "args": cmd.args, "result": result_future})
#         output = await result_future
#         results.append({"cs_pin": cmd.cs_pin, "output": output})
#     return {"status": True, "results": results}

@app.post("/stepper")
async def control_stepper(batch: StepperCommandBatch):
    results = []
    for cmd in batch.commands:
        result_future = asyncio.get_running_loop().create_future()
        await stepper_queue.put({
            "direction": str(cmd.direction),  # force string copy
            "steps": int(cmd.steps),          # force int copy
            "result": result_future
        })
        results.append(result_future)
    await asyncio.gather(*results)
    return {"status": True, "results": results}


# Serve the static files (Svelte app)
# Ensure these files are mounted last, otherwise POST requests may fail
app.mount("/", StaticFiles(directory=svelte_frontend, html=True), name="build")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7070, log_level="info")
