#!/usr/bin/env python

"""
Venus OS D-Bus driver for a go-eCharger (hardware v2, local HTTP API v1).

Publishes the charger as com.victronenergy.evcharger, so it shows up in the GUI and on VRM.

Modes (/Mode):
  0 Manual: /StartStop and /SetCurrent from the GUI or VRM control the charger.
  1 Auto:   the charger is controlled by Home Assistant (PV surplus, cheap energy, ...),
            this driver only reports and rejects /StartStop and /SetCurrent writes.
  2 Scheduled is not supported.
"""

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
from gi.repository import GLib
import time
import requests  # for http GET
import configparser  # for config/ini file
import dbus

# our own packages from victron
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

VERSION = '2.0'

MODE_MANUAL = 0
MODE_AUTO = 1

# /Status values of the Venus evcharger API
STATUS_DISCONNECTED = 0
STATUS_CONNECTED = 1
STATUS_CHARGING = 2
STATUS_CHARGED = 3
STATUS_WAITING_FOR_SUN = 4
STATUS_WAITING_FOR_START = 6
STATUS_GROUND_FAULT = 8
STATUS_RESIDUAL_CURRENT = 11

# go-e 'err' codes mapped to /Status
ERROR_STATUS = {
    1: STATUS_RESIDUAL_CURRENT,  # RCCB
    8: STATUS_GROUND_FAULT,      # no ground
}

MIN_CURRENT = 6


def _get_config():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config


class DbusGoeChargerService:
    def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
        config = _get_config()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        hardware_version = int(config['DEFAULT']['HardwareVersion'])
        ac_position = int(config['DEFAULT']['AcPosition'])
        pause_between_requests = int(config['ONPREMISE']['PauseBetweenRequests'])  # in ms
        self._host = config['ONPREMISE']['Host']

        if config['DEFAULT']['AccessType'] != 'OnPremise':
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
        if pause_between_requests <= 20:
            raise ValueError("Pause between requests must be greater than 20")

        self._last_update = 0
        self._charging_time = 0.0

        # the mode survives restarts of the driver
        self._settings = SettingsDevice(
            bus=dbus.SystemBus(),
            supportedSettings={
                'mode': ['/Settings/GoeCharger/%d/Mode' % deviceinstance, MODE_AUTO, MODE_MANUAL, MODE_AUTO],
            },
            eventCallback=self._on_setting_changed,
        )

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)

        # data of the charger, it might not be reachable yet
        data = self._get_data() or {}

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', VERSION + ' on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', productname)
        self._dbusservice.add_path('/Model', 'go-eCharger HW v%d' % hardware_version)
        self._dbusservice.add_path('/FirmwareVersion', str(data.get('fwv') or '') or None)
        self._dbusservice.add_path('/Serial', data.get('sse') or None)
        self._dbusservice.add_path('/HardwareVersion', hardware_version)
        self._dbusservice.add_path('/Connected', 1 if data else 0)
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', ac_position)
        self._dbusservice.add_path('/Status', None)
        self._dbusservice.add_path('/Ac/PhaseCount', None)

        for path, settings in paths.items():
            self._dbusservice.add_path(path, settings['initial'], gettextcallback=settings['textformat'])

        # control paths
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        self._dbusservice.add_path('/Mode', self._settings['mode'], writeable=True, onchangecallback=self._handle_changed_value)
        self._dbusservice.add_path('/StartStop', None, writeable=True, onchangecallback=self._handle_changed_value)
        self._dbusservice.add_path('/SetCurrent', None, writeable=True, gettextcallback=_a, onchangecallback=self._handle_changed_value)
        self._dbusservice.add_path('/MaxCurrent', None, gettextcallback=_a)

        if data:
            self._publish(data)

        self._dbusservice.register()

        GLib.timeout_add(pause_between_requests, self._update)
        sign_of_life = int(config['DEFAULT'].get('SignOfLifeLog') or 0)
        if sign_of_life > 0:
            GLib.timeout_add(sign_of_life * 60 * 1000, self._sign_of_life)

    # --- charger access -------------------------------------------------------------------------

    def _get_data(self):
        try:
            response = requests.get(url="http://%s/status" % self._host, timeout=2)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.debug("go-eCharger not available: %s", e)
            return None

    def _set_value(self, parameter, value):
        """Set a charger parameter (API v1), return the new status if the charger confirms it."""
        url = "http://%s/mqtt?payload=%s=%s" % (self._host, parameter, value)
        try:
            response = requests.get(url=url, timeout=2)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logging.error("Failed to set go-eCharger %s=%s: %s", parameter, value, e)
            return None

        if str(data.get(parameter)) != str(value):
            logging.warning("go-eCharger did not set %s to %s, it reports %s", parameter, value, data.get(parameter))
            return None
        return data

    # --- reporting --------------------------------------------------------------------------------

    def _status(self, data):
        err = int(data.get('err', 0))
        if err:
            return ERROR_STATUS.get(err, STATUS_CONNECTED)

        car = int(data['car'])
        if car == 1:  # ready, no vehicle
            return STATUS_DISCONNECTED
        if car == 2:  # vehicle charging
            return STATUS_CHARGING
        if car == 4:  # charge finished, vehicle still connected
            return STATUS_CHARGED
        # car 3: vehicle connected, waiting
        if int(data['alw']) == 0:
            return STATUS_WAITING_FOR_SUN if self._dbusservice['/Mode'] == MODE_AUTO else STATUS_WAITING_FOR_START
        return STATUS_CONNECTED

    def _publish(self, data):
        nrg = data['nrg']
        s = self._dbusservice

        s['/Ac/Voltage'] = int(nrg[0])
        # nrg[7..9] in 0.1kW, phases wired in reverse order
        s['/Ac/L3/Power'] = int(nrg[7]) * 100
        s['/Ac/L2/Power'] = int(nrg[8]) * 100
        s['/Ac/L1/Power'] = int(nrg[9]) * 100
        s['/Ac/Power'] = int(nrg[11]) * 10  # 0.01kW

        # actual charging current, max of the phase currents in 0.1A
        current = max(nrg[4], nrg[5], nrg[6]) / 10.0
        s['/Current'] = current
        s['/ChargingCurrent'] = current

        # phases after the contactor (pha bits 3..5)
        s['/Ac/PhaseCount'] = bin((int(data.get('pha', 0)) >> 3) & 7).count('1') or None

        # energy of the current session (dws in deka-watt-seconds), total energy (eto in 0.1kWh)
        s['/Ac/Energy/Forward'] = round(int(data['dws']) / 360000.0, 2)
        s['/Ac/Energy/Total'] = int(data['eto']) / 10.0

        s['/StartStop'] = int(data['alw'])
        s['/SetCurrent'] = int(data['amx'])
        s['/MaxCurrent'] = int(data['ama'])
        s['/MCU/Temperature'] = int(data['tmp'])

        # charging time of the session
        now = time.time()
        car = int(data['car'])
        if car == 2 and self._last_update > 0:
            self._charging_time += now - self._last_update
        elif car == 1:
            self._charging_time = 0
        s['/ChargingTime'] = int(self._charging_time)

        s['/Status'] = self._status(data)

    def _update(self):
        try:
            data = self._get_data()
            if data is None:
                if self._dbusservice['/Connected']:
                    logging.warning("go-eCharger at %s not reachable", self._host)
                self._dbusservice['/Connected'] = 0
                self._last_update = 0
                return True

            if not self._dbusservice['/Connected']:
                logging.info("go-eCharger at %s reachable", self._host)
            self._dbusservice['/Connected'] = 1
            self._publish(data)

            self._dbusservice['/UpdateIndex'] = (self._dbusservice['/UpdateIndex'] + 1) % 256
            self._last_update = time.time()
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        return True

    def _sign_of_life(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s", self._last_update)
        logging.info("Last '/Ac/Power': %s", self._dbusservice['/Ac/Power'])
        logging.info("--- End: sign of life ---")
        return True

    # --- control ----------------------------------------------------------------------------------

    def _on_setting_changed(self, setting, oldvalue, newvalue):
        if setting == 'mode':
            self._dbusservice['/Mode'] = newvalue

    def _handle_changed_value(self, path, value):
        logging.info("GUI/VRM changed %s to %s", path, value)

        try:
            if path == '/Mode':
                mode = int(value)
                if mode not in (MODE_MANUAL, MODE_AUTO):
                    logging.warning("Mode %s is not supported, only 0 (Manual) and 1 (Auto)", value)
                    return False
                self._settings['mode'] = mode
                logging.info("Mode %s", "Auto, Home Assistant controls the charger" if mode == MODE_AUTO else "Manual")
                return True

            if self._dbusservice['/Mode'] != MODE_MANUAL:
                logging.warning("%s can only be changed in Manual mode", path)
                return False

            if path == '/StartStop':
                start = int(value)
                if start not in (0, 1):
                    return False
                data = self._set_value('alw', start)
            elif path == '/SetCurrent':
                maximum = self._dbusservice['/MaxCurrent'] or 32
                current = max(MIN_CURRENT, min(int(round(float(value))), maximum))
                data = self._set_value('amx', current)
            else:
                return False
        except (TypeError, ValueError):
            return False

        if data is None:
            return False
        # publish what the charger reports, e.g. the clamped current
        GLib.idle_add(self._publish, data)
        return True


def main():
    config = _get_config()
    logging_level = config["DEFAULT"]["Logging"].upper()

    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging_level,
                        handlers=[
                            RotatingFileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__))), maxBytes=10000),
                            logging.StreamHandler()
                        ])

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        _w = lambda p, v: (str(round(v, 1)) + 'W')
        _v = lambda p, v: (str(round(v, 1)) + 'V')
        _degC = lambda p, v: (str(v) + '°C')
        _s = lambda p, v: (str(v) + 's')

        DbusGoeChargerService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Ac/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/Ac/Energy/Total': {'initial': 0, 'textformat': _kwh},
                '/ChargingTime': {'initial': 0, 'textformat': _s},
                '/Ac/Voltage': {'initial': 0, 'textformat': _v},
                '/Current': {'initial': 0, 'textformat': _a},
                '/ChargingCurrent': {'initial': 0, 'textformat': _a},
                '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
            }
        )

        logging.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
        GLib.MainLoop().run()
    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()
