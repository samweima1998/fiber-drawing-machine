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
from shutil import which
from datetime import datetime
import time


logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change "*" to your frontend URL for better security
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Ensure POST is included
    allow_headers=["*"],
)
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
    contact_time: float
    drawing_pressure: float
    drawing_height: float
    curing_temperature: float
    curing_intensity: float
    curing_time: float
    stretching_delay: float

# Global lock to prevent overlapping /send_data executions
send_data_lock = asyncio.Lock()

# Track current input data for live updates and a lock to protect it
current_input_data: Optional[InputData] = None
current_input_lock = asyncio.Lock()

# Global variable to store the latest temperature
latest_temperature: float = 0.0

# Global variable to store the latest pressure (temperature-adjusted)
latest_pressure = 0.0

# Global variable to store the latest status
latest_status: str = "Idle"
# Global flag to skip waiting conditions
skip_waiting_flag = False

record_proc: Optional[asyncio.subprocess.Process] = None
record_meta = {"out": None, "fps": None, "stopped_mtx": False}
record_lock = asyncio.Lock()

class RecordStartReq(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: int = 17_000_000
    af_mode: str = "manual"            # "manual" | "auto" | "continuous"
    lens_position: Optional[float] = None  # e.g. 0=infinity, 0.5≈2m, 2≈0.5m
    meters: Optional[float] = None         # alternative: desired distance in meters

async def _svc_is_active(name: str) -> bool:
    p = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", "--quiet", name
    )
    await p.wait()
    return p.returncode == 0

async def _svc_action(action: str, name: str):
    # Requires passwordless sudo for these commands; see notes below.
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", action, name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await p.wait()

@app.post("/record/start")
async def record_start(body: RecordStartReq):
    global record_proc, record_meta
    async with record_lock:
        if record_proc and record_proc.returncode is None:
            raise HTTPException(status_code=409, detail="Recording already running")

        # Stop MediaMTX if active (so it doesn't hold the camera)
        stopped_mtx = False
        try:
            if await _svc_is_active("mediamtx"):
                await _svc_action("stop", "mediamtx")
                stopped_mtx = True
        except Exception:
            pass

        # (Optional on Bullseye; hardware encoder module)
        try:
            await asyncio.create_subprocess_exec(
                "sudo", "modprobe", "bcm2835_codec",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        except Exception:
            pass

        # Prepare output
        Path.home().joinpath("videos").mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_h264 = Path.home()/ "videos" / f"rec_{body.width}x{body.height}_{body.fps}fps_{ts}.h264"

        # Fallback to MediaMTX defaults if AF not provided
        try:
            need_af_mode = (not getattr(body, "af_mode", None))
            need_lens = (body.meters is None and body.lens_position is None)
            if need_af_mode or need_lens:
                import yaml, pathlib
                yml = pathlib.Path("/home/pi/mediamtx/mediamtx.yml")
                if yml.exists():
                    cfg = yaml.safe_load(yml.read_text()) or {}
                    cam = (cfg.get("paths") or {}).get("cam") or {}
                    if need_af_mode:
                        body.af_mode = cam.get("rpiCameraAfMode", "manual")
                    if need_lens and cam.get("rpiCameraLensPosition") is not None:
                        body.lens_position = float(cam["rpiCameraLensPosition"])
        except Exception as e:
            logging.warning(f"MTX defaults not loaded: {e}")

        # 3) before spawning libcamera-vid, derive lens position
        lens_pos = None
        if body.meters is not None:
            lens_pos = 0.0 if body.meters == 0 else round(1.0 / float(body.meters), 3)
        elif body.lens_position is not None:
            lens_pos = float(body.lens_position)

        # 4) add focus flags to the command
        cmd = [
            "libcamera-vid", "--nopreview", "-t", "0", "--codec", "h264", "--inline",
            "--width", str(body.width), "--height", str(body.height),
            "--framerate", str(body.fps),
            "--bitrate", str(body.bitrate),
            "--intra", str(body.fps * 2),
        ]
        # focus
        if body.af_mode == "manual":
            cmd += ["--autofocus-mode", "manual"]
            if lens_pos is not None:
                cmd += ["--lens-position", f"{lens_pos}"]
        elif body.af_mode == "continuous":
            cmd += ["--autofocus-mode", "continuous"]
        elif body.af_mode == "auto":
            cmd += ["--autofocus-mode", "auto", "--autofocus"]

        cmd += ["-o", str(out_h264)]

        record_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Give it a moment to fail fast if there's a camera/format error
        await asyncio.sleep(1.0)
        if record_proc.returncode is not None:
            err = (await record_proc.stderr.read()).decode(errors="ignore")
            # Try to restart MediaMTX if we stopped it
            if stopped_mtx:
                await _svc_action("start", "mediamtx")
            raise HTTPException(status_code=500, detail=f"libcamera-vid failed: {err.strip()}")

        record_meta.update({"out": str(out_h264), "fps": body.fps, "stopped_mtx": stopped_mtx})
        return {"status": "recording", "raw": str(out_h264), "fps": body.fps}

@app.post("/record/stop")
async def record_stop():
    global record_proc, record_meta
    async with record_lock:
        if not record_proc or record_proc.returncode is not None:
            raise HTTPException(status_code=409, detail="No active recording")

        # Stop gracefully
        record_proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(record_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            record_proc.kill()
            await record_proc.wait()

        out_h264 = record_meta.get("out")
        fps = record_meta.get("fps", 30)
        if not out_h264 or not Path(out_h264).exists():
            # Try to restart MediaMTX if we stopped it
            if record_meta.get("stopped_mtx"):
                await _svc_action("start", "mediamtx")
            raise HTTPException(status_code=500, detail="Raw .h264 not found")

        out_mp4 = str(Path(out_h264).with_suffix(".mp4"))

        # Remux to mp4 (prefer MP4Box; fallback to ffmpeg)
        if which("MP4Box"):
            p = await asyncio.create_subprocess_exec(
                "MP4Box", "-quiet", "-add", f"{out_h264}:fps={fps}", "-new", out_mp4,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            await p.wait()
            if p.returncode != 0:
                # Fallback to ffmpeg
                p = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-r", str(fps), "-f", "h264", "-i", out_h264, "-c", "copy", out_mp4
                )
                await p.wait()
                if p.returncode != 0:
                    if record_meta.get("stopped_mtx"):
                        await _svc_action("start", "mediamtx")
                    raise HTTPException(status_code=500, detail="Remux to MP4 failed")
        else:
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-r", str(fps), "-f", "h264", "-i", out_h264, "-c", "copy", out_mp4
            )
            await p.wait()
            if p.returncode != 0:
                if record_meta.get("stopped_mtx"):
                    await _svc_action("start", "mediamtx")
                raise HTTPException(status_code=500, detail="Remux to MP4 failed")

        # Optionally restart MediaMTX if we stopped it
        if record_meta.get("stopped_mtx"):
            await _svc_action("start", "mediamtx")

        # reset state
        record_proc = None
        record_meta = {"out": None, "fps": None, "stopped_mtx": False}

        return {"status": "stopped", "mp4": out_mp4}


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

async def pressure_streamer(process, stop_event, interval=0.1):
    """Continuously stream latest_pressure to the stepper process."""
    global latest_pressure
    logging.info("Pressure streamer started")
    while not stop_event.is_set():
        try:
            process.stdin.write(f"{latest_pressure}\n".encode())
            await process.stdin.drain()
        except Exception as e:
            logging.error(f"Pressure streamer error: {e}")
            break
        await asyncio.sleep(interval)  # 20ms = 50Hz


async def sleep_with_skip(duration: float, phase_name: str = "", check_interval: float = 0.1) -> bool:
    """Sleep in small increments and return True if skip_waiting_flag was triggered.

    Returns True when the sleep was interrupted by a skip request, False otherwise.
    """
    global skip_waiting_flag
    start = time.monotonic()
    while time.monotonic() - start < duration:
        if skip_waiting_flag:
            logging.info(f"Skipping {phase_name} due to user request.")
            skip_waiting_flag = False
            return True
        await asyncio.sleep(min(check_interval, duration - (time.monotonic() - start)))
    return False
        
@app.get("/status")
async def get_status():
    async with current_input_lock:
        target = current_input_data.dict() if current_input_data else None
    status = {
        "current_temperature": latest_temperature,
        "current_pressure": latest_pressure,
        "status": latest_status,
        "target_params": target,
    }
    return status

@app.post("/send_data")
async def receive_data(data: InputData):
    global latest_status, latest_temperature, latest_pressure, skip_waiting_flag, current_input_data
    if send_data_lock.locked():
        raise HTTPException(status_code=429, detail="Previous /send_data still in progress")

    async with send_data_lock:
        # store current input so it can be updated live
        async with current_input_lock:
            current_input_data = data
        try:
            # Wait until temperature condition is met (live-updating target)
            latest_status = "Waiting for drawing temperature"
            while True:
                async with current_input_lock:
                    target_temp = current_input_data.drawing_temperature if current_input_data else data.drawing_temperature
                if abs(latest_temperature - target_temp) <= 0.5:
                    break
                if skip_waiting_flag:
                    logging.info("Skipping waiting for drawing temperature due to user request.")
                    skip_waiting_flag = False
                    break
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
            while latest_pressure > -150:
                if skip_waiting_flag:
                    logging.info("Skipping waiting for pressure due to user request.")
                    skip_waiting_flag = False
                    break
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
            # Make this wait interruptible by /skip_waiting (live-updates)
            async with current_input_lock:
                contact_time_val = current_input_data.contact_time if current_input_data else data.contact_time
            skipped = await sleep_with_skip(contact_time_val, phase_name="Maintaining contact")
            if skipped:
                latest_status = "Maintaining contact skipped"

            #Pressure sensitive drawing
            result_future_guarded_move = asyncio.get_running_loop().create_future()
            async with current_input_lock:
                steps = int(current_input_data.drawing_height * 6250)
                pressure_threshold = current_input_data.drawing_pressure
            await stepper_queue.put({
                "command": "GUARDED_MOVE",
                "direction": "BACKWARD",
                "steps": steps, # Convert mm to steps
                "interval_us": 200,
                "pressure_threshold": pressure_threshold,
                "result": result_future_guarded_move
            })
            latest_status = "Drawing"
            await result_future_guarded_move
            logging.info("Drawing complete.")
            latest_status = "Drawing complete"

            # Wait until temperature condition is met (live-updating target)
            latest_status = "Waiting for curing temperature"
            while True:
                async with current_input_lock:
                    target_curing_temp = current_input_data.curing_temperature if current_input_data else data.curing_temperature
                if abs(latest_temperature - target_curing_temp) <= 0.5:
                    break
                if skip_waiting_flag:
                    logging.info("Skipping waiting for curing temperature due to user request.")
                    skip_waiting_flag = False
                    break
                await asyncio.sleep(0.1)
            logging.info("curing temperature reached.")

            # Start curing (use live-updating intensity)
            if ser and ser.is_open:
                async with current_input_lock:
                    intensity = current_input_data.curing_intensity
                ser.reset_input_buffer()  # Clear any old data
                ser.write(f"start curing {intensity}\n".encode())
                ser.flush()
                logging.info(f"Sent 'start curing {intensity}' command to Arduino.")
                latest_status = "Gentle curing"

            # Wait for stretching time, but allow user to skip gentle curing (live-updates)
            async with current_input_lock:
                sd = current_input_data.stretching_delay if current_input_data else data.stretching_delay
                ct = current_input_data.curing_time if current_input_data else data.curing_time
            skipped = await sleep_with_skip(min(sd, ct), phase_name="Gentle curing")
            if skipped:
                # If skipping while curing, tell the Arduino to stop curing immediately and finish early
                if ser and ser.is_open:
                    ser.reset_input_buffer()  # Clear any old data
                    ser.write(b"stop curing\n")
                    ser.flush()
                    logging.info("Sent 'stop curing' command to Arduino due to skip.")
                latest_status = "Curing stopped"
                return {"status": "skipped", "message": "Curing skipped by user"}
            #Pressure sensitive stretching
            logging.info("Starting pressure-sensitive stretching.")
            result_future_guarded_move = asyncio.get_running_loop().create_future()
            async with current_input_lock:
                steps_stretch = int(current_input_data.drawing_height * 625)
                pressure_threshold = current_input_data.drawing_pressure
            await stepper_queue.put({
                "command": "GUARDED_MOVE",
                "direction": "BACKWARD",
                "steps": steps_stretch, # 10% of drawing height for stretching
                "interval_us": 200,
                "pressure_threshold": pressure_threshold,
                "result": result_future_guarded_move
            })
            latest_status = "Stretching"
            await result_future_guarded_move
            latest_status = "Gentle curing"

            # Allow interrupting the remaining curing time (live-updates)
            async with current_input_lock:
                remaining = max(0, current_input_data.curing_time - current_input_data.stretching_delay)
            skipped = await sleep_with_skip(remaining, phase_name="Gentle curing")
            if skipped:
                if ser and ser.is_open:
                    ser.reset_input_buffer()  # Clear any old data
                    ser.write(b"stop curing\n")
                    ser.flush()
                    logging.info("Sent 'stop curing' command to Arduino due to skip.")
                latest_status = "Curing stopped"
                return {"status": "skipped", "message": "Curing skipped by user"}

            if ser and ser.is_open:
                ser.reset_input_buffer()  # Clear any old data
                ser.write(b"stop curing\n")
                ser.flush()
                logging.info("Sent 'stop curing' command to Arduino.")
                latest_status = "Curing complete"

        except Exception as e:
            logging.error(f"Error in /send_data: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            # Clear current input data when run finishes, so /update_param fails when not running
            async with current_input_lock:
                current_input_data = None


@app.post("/update_param")
async def update_param(body: dict):
    """
    Update a single parameter on the currently running /send_data run.
    Expects JSON: { "param": "<field_name>", "value": <number> }
    """
    param = body.get("param")
    if not param:
        raise HTTPException(status_code=400, detail="Missing 'param' field")
    if "value" not in body:
        raise HTTPException(status_code=400, detail="Missing 'value' field")
    async with current_input_lock:
        if current_input_data is None:
            raise HTTPException(status_code=409, detail="No active /send_data run")
        if not hasattr(current_input_data, param):
            raise HTTPException(status_code=400, detail="Unknown parameter")
        try:
            current_val = getattr(current_input_data, param)
            new_val = type(current_val)(body["value"])
            setattr(current_input_data, param, new_val)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid value: {e}")
    logging.info(f"Updated parameter {param} to {getattr(current_input_data, param)}")
    return {"status": "updated", "param": param, "value": getattr(current_input_data, param)}


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

                        # Start pressure streaming
                        logging.info("Starting pressure streamer for GUARDED_MOVE")
                        stop_event = asyncio.Event()
                        streamer_task = asyncio.create_task(pressure_streamer(process, stop_event))

                        # Wait for DONE from stepper process
                        while True:
                            # Read one line from stdout (blocking)
                            line = await process.stdout.readline()
                            if not line:
                                break
                            decoded = line.decode().strip()
                            logging.info(f"Stepper subprocess output: {decoded}")
                            if decoded == "DONE":
                                break

                        # Stop pressure streaming
                        stop_event.set()
                        await streamer_task

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
                        # Read one line from stdout (blocking)
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

    # Initialize serial connection for arduino
    try:
        ser = serial.Serial('/dev/serial/by-id/usb-Arduino_LLC_Arduino_NANO_33_IoT_7CB63C1050304D48502E3120FF191434-if00', 115200, timeout=1)
        logging.info("Serial connection to arduino established.")
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

# Serve the static files (Svelte app)
app.mount("/static", StaticFiles(directory=svelte_frontend, html=True), name="static")

# Serve the Svelte index.html for the root route
@app.get("/")
async def serve_svelte():
    return FileResponse(svelte_frontend / "index.html")

# Endpoint to set skip_waiting_flag
@app.post("/skip_waiting")
async def skip_waiting():
    global skip_waiting_flag
    skip_waiting_flag = True
    logging.info("Skip waiting triggered by user.")
    return {"status": "skipped"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7070, log_level="info", access_log=False)
