#!/usr/bin/env python

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import time
import requests # for http GET
import configparser # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
    acPosition = int(config['DEFAULT']['AcPosition'])
    pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests']) # in ms

    if pauseBetweenRequests <= 20:
      raise ValueError("Pause between requests must be greater than 20")

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

    paths_wo_unit = [
      '/Status',  # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
    ]

    #get data from go-eCharger
    data = self._getGoeChargerData('sse,fwv')

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xFFFF)
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', productname)
    if data:
       fwv = data.get('fwv', '')
       try:
           fwv = int(str(fwv).replace('.', ''))
       except:
           pass
       self._dbusservice.add_path('/FirmwareVersion', fwv)
       self._dbusservice.add_path('/Serial', data.get('sse', ''))
    self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/Position', acPosition)

    # add paths without units
    for path in paths_wo_unit:
      self._dbusservice.add_path(path, None)

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # register the service
    self._dbusservice.register()

    # last update
    self._lastUpdate = 0

    # charging time in float
    self._chargingTime = 0.0

    # add _update function 'timer'
    gobject.timeout_add(pauseBetweenRequests, self._update)

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config

  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']

    if not value:
        value = 0

    return int(value)

  def _getGoeChargerStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
      URL = "http://%s/status" % (config['ONPREMISE']['Host'])
    else:
      raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL

  def _getGoeChargerMqttPayloadUrl(self, parameter, value):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
        URL = "http://%s/mqtt?payload=%s=%s" % (config['ONPREMISE']['Host'], parameter, value)
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL

  def _setGoeChargerValue(self, parameter, value):
    try:
      URL = self._getGoeChargerMqttPayloadUrl(parameter, str(value))
      request_data = requests.get(url=URL, timeout=2)

      if not request_data:
        logging.warning("No response from go-eCharger - %s" % (URL))
        return False

      json_data = request_data.json()
      if not json_data:
        logging.warning("Converting response to JSON failed")
        return False

      if str(json_data.get(parameter)) == str(value):
        return True
      else:
        logging.warning("go-eCharger parameter %s not set to %s" % (parameter, str(value)))
        return False
    except Exception as e:
      logging.error("Failed to set go-eCharger value %s=%s: %s" % (parameter, value, e))
      return False

  def _getGoeChargerData(self, filter):
    URL = self._getGoeChargerStatusUrl()
    try:
       request_data = requests.get(url=URL, timeout=1)
    except Exception:
       return None

    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (URL))

    json_data = request_data.json()
    if not json_data:
        raise ValueError("Converting response to JSON failed")

    return json_data

  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("--- End: sign of life ---")
    return True

  def _update(self):
    try:
       data = self._getGoeChargerData('nrg,eto,wh,alw,amp,ama,car,tmp,tma,amx')

       if data is not None:
          # send power data to DBus
          self._dbusservice['/Ac/Voltage'] = int(data['nrg'][0])
          self._dbusservice['/Ac/L3/Power'] = int(data['nrg'][7]) * 100
          self._dbusservice['/Ac/L2/Power'] = int(data['nrg'][8]) * 100
          self._dbusservice['/Ac/L1/Power'] = int(data['nrg'][9]) * 100
          self._dbusservice['/Ac/Power'] = int(data['nrg'][11]) * 10

          # Actual charging current (max of L1, L2, L3 currents in Amps)
          charging_current = max(data['nrg'][4], data['nrg'][5], data['nrg'][6]) / 10.0
          self._dbusservice['/Current'] = charging_current
          self._dbusservice['/ChargingCurrent'] = charging_current

          # eto in v1 is Total charged energy in 0.1kWh
          self._dbusservice['/Ac/Energy/Forward'] = float(data['eto']) / 10.0

          # Control & setpoints
          self._dbusservice['/StartStop'] = int(data['alw'])
          self._dbusservice['/SetCurrent'] = int(data['amx'])
          self._dbusservice['/MaxCurrent'] = int(data['ama'])

          # Update chargingTime
          timeDelta = time.time() - self._lastUpdate
          if int(data['car']) == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)

          if '/MCU/Temperature' in self._dbusservice:
             self._dbusservice['/MCU/Temperature'] = int(data['tmp'])

          # Status mapping
          status = 0
          if int(data['car']) == 1:
            status = 0
          elif int(data['car']) == 2:
            status = 2
          elif int(data['car']) == 3:
            status = 6
          elif int(data['car']) == 4:
            status = 3
          self._dbusservice['/Status'] = status

          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("---")

          index = self._dbusservice['/UpdateIndex'] + 1
          if index > 255:
            index = 0
          self._dbusservice['/UpdateIndex'] = index

          self._lastUpdate = time.time()
       else:
          logging.debug("Wallbox is not available")

    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)

    return True

  def _handlechangedvalue(self, path, value):
    logging.info("someone else updated %s to %s" % (path, value))

    if path == '/SetCurrent':
      return self._setGoeChargerValue('amx', int(round(float(value))))
    elif path == '/StartStop':
      return self._setGoeChargerValue('alw', int(value))
    elif path == '/MaxCurrent':
      return self._setGoeChargerValue('ama', int(round(float(value))))
    elif path == '/Mode':
      try:
        mode = int(value)
        if mode in (0, 1):
          self._dbusservice['/Mode'] = mode
          logging.info("Updated /Mode to %d", mode)
          return True
        else:
          logging.warning("Invalid /Mode value %s received (must be 0 or 1)", value)
          return False
      except (ValueError, TypeError):
        return False
    else:
      logging.info("mapping for evcharger path %s does not exist" % (path))
      return False


def main():
  config = configparser.ConfigParser()
  config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
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

      pvac_output = DbusGoeChargerService(
        servicename='com.victronenergy.evcharger',
        paths={
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          '/ChargingTime': {'initial': 0, 'textformat': _s},

          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Current': {'initial': 0, 'textformat': _a},
          '/ChargingCurrent': {'initial': 0, 'textformat': _a},
          '/SetCurrent': {'initial': 0, 'textformat': _a},
          '/MaxCurrent': {'initial': 0, 'textformat': _a},
          '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
          '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))},
          '/Mode': {'initial': 0, 'textformat': lambda p, v: (str(v))}
        }
      )

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)

if __name__ == "__main__":
  main()