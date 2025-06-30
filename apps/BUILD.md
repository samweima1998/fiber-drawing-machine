```sh
sudo apt-get install git build-essential
cd multi-half-bridge/src/framework/raspberrypi
chmod +x ./install_requirements.sh
./install_requirements.sh


sudo reboot
```

After reboot:

```sh
cd multi-half-bridge/src/framework/raspberrypi
make clean
make examples/control
```

```sh
../../../build/control
```


```sh
cd
```