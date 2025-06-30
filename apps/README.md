# RPI ZERO 2W

Using Raspberri Pi Imager, install on a micro-SD card: 
    Raspberry Pi OS (Legacy , 32-bit) Bullseye with security updates and no desktop environment
    Edit configuration to set device name and wifi settings
Using another Linux device, find the rootfs partition and edit /etc/rc.local:
    sudo nano /mnt/rpi-root/etc/rc.local
Add, before "exit 0":
    # This line ensures wifi connectivity when on battery power
    iw dev wlan0 set power_save off
Save and unmount:
    sudo umount /mnt/rpi-root
Insert micro-SD into RPi Zero
Boot (first boot may take some time)

```sh
#Open the sudoers file using the visudo command:
sudo visudo
#Find the line that starts with: 
    root    ALL=(ALL:ALL) ALL
#Below it, add:
    username ALL=(ALL) NOPASSWD:ALL

sudo apt-get update

sudo apt-get install git
sudo apt-get install python3-pip

git clone https://github.com/samweima1998/multi-half-bridge.git

#Manually get and install bcm2835
wget http://www.airspayce.com/mikem/bcm2835/bcm2835-1.71.tar.gz
tar -xzf bcm2835-1.71.tar.gz
cd bcm2835-1.71
./configure
make
sudo make install
cd

cd multi-half-bridge
pip3 install -r apps/requirements.txt

# Test run Server
python apps/server.py
# Visit `http://<raspberrypi-ip>:7070` from a browser on the same network and check if it displays the web page
```
# Auto Launch Server on Boot

To have a Python server script launch automatically when your Raspberry Pi boots, you need to create a service that runs at startup. This can be achieved using `systemd`, which is the initialization system and service manager that is widely used in Linux distributions, including Raspberry Pi OS.

Below are the steps to create a systemd service that will start your Python server when the Raspberry Pi boots:

### Step 1: Create a systemd Service File

1. Create a service file under `/etc/systemd/system/`. For example, name it `python_server.service`:
```bash
sudo nano /etc/systemd/system/python_server.service
```

2. Add the following content to the service file:
```ini
[Unit]
Description=Python Server
After=network.target

[Service]
ExecStart=/usr/bin/python /home/pi/multi-half-bridge/apps/server.py
WorkingDirectory=/home/pi
StandardOutput=inherit
StandardError=inherit
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

3. Save and exit the file (using CTRL+X, then Y and Enter in nano).

### Step 2: Enable and Start the Service

1. Reload the systemd manager configuration:
```bash
sudo systemctl daemon-reload
```

2. Enable the new service to start on boot:
```bash
sudo systemctl enable python_server.service
```

3. Start the service immediately:
```bash
sudo systemctl start python_server.service
```

### Step 3: Check the Status of Your Service

To ensure that the service is active and running, you can use:
```bash
sudo systemctl status python_server.service
```

If everything is set up correctly, your Python server should now automatically start when your Raspberry Pi boots up.

### Troubleshooting

If your server doesn't start properly, you can check the logs for errors:
```bash
sudo journalctl -u python_server.service --no-pager | tail -n 50

```

