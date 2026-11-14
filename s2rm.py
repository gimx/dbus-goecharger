"""
S2 resource manager (EN 50491-12-2) for the go-eCharger, so the Venus OS Opportunity Loads
(the CEM, com.victronenergy.opportunityloads) can control it.

D-Bus side: the item at /S2/0/Rm implements the com.victronenergy.S2 interface. The CEM calls
the methods Discover, Connect, Disconnect, Message and KeepAlive, the RM answers with the signals
Message and Disconnect (client id, payload).

S2 side: Operation Mode Based Control (OMBC) with a ladder of operation modes, "Standby" and one
mode per ampere from MIN_CURRENT to the charger's maximum, on the phases the car uses. Like the
Victron EVCS, the power of a mode is current x 230 V per phase, Opportunity Loads corrects it
with the actual voltage ("EVCS quirks", it reads /GxAutoMode/ChargingCurrent).
Hysteresis timers keep the charger on for at least ON_MIN_S and off for at least OFF_MIN_S.

Written for dbus-python + GLib (velib vedbus), all calls run in the GLib main loop.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import dbus
import dbus.lowlevel
import dbus.service
from gi.repository import GLib

from vedbus import VeDbusItemExport

from s2python.common import (
    Commodity,
    CommodityQuantity,
    ControlType,
    Duration,
    EnergyManagementRole,
    Handshake,
    HandshakeResponse,
    PowerMeasurement,
    PowerRange,
    PowerValue,
    ReceptionStatus,
    ReceptionStatusValues,
    ResourceManagerDetails,
    Role,
    RoleType,
    SelectControlType,
    Timer,
    Transition,
)
from s2python.ombc import OMBCInstruction, OMBCOperationMode, OMBCStatus, OMBCSystemDescription
from s2python.s2_parser import S2Parser
from s2python.s2_validation_error import S2ValidationError
from s2python.version import S2_VERSION

IFACE = 'com.victronenergy.S2'

MIN_CURRENT = 6
NOMINAL_VOLTAGE = 230

# hysteresis
ON_MIN_S = 300    # stay on at least, avoids short charging sessions
OFF_MIN_S = 120   # stay off at least
STEP_MIN_S = 15   # between current steps

PHASES = (CommodityQuantity.ELECTRIC_POWER_L1, CommodityQuantity.ELECTRIC_POWER_L2, CommodityQuantity.ELECTRIC_POWER_L3)

logger = logging.getLogger('s2rm')


class S2RmItem(VeDbusItemExport):
    """The /S2/0/Rm item: a normal bus item (value "Ready" or "CEM: <client>") plus the S2 interface."""

    rm = None

    def emit(self, member, client_id, payload):
        # emitted by hand: dbus-python keys methods and signals by name, and the S2 interface has
        # a method and a signal of the same name (Message, Disconnect)
        msg = dbus.lowlevel.SignalMessage(self.__dbus_object_path__, IFACE, member)
        msg.append(client_id or '', payload, signature='ss')
        self.connection.send_message(msg)

    @dbus.service.method(IFACE, out_signature='b')
    def Discover(self):
        return self.rm is not None

    @dbus.service.method(IFACE, in_signature='si', out_signature='b')
    def Connect(self, client_id, keep_alive_interval):
        return self.rm.on_connect(str(client_id), int(keep_alive_interval))

    @dbus.service.method(IFACE, in_signature='s')
    def Disconnect(self, client_id):
        self.rm.on_disconnect(str(client_id))

    @dbus.service.method(IFACE, in_signature='ss')
    def Message(self, client_id, message):
        self.rm.on_message(str(client_id), str(message))

    @dbus.service.method(IFACE, in_signature='s', out_signature='b')
    def KeepAlive(self, client_id):
        return self.rm.on_keep_alive(str(client_id))


class S2ResourceManager:
    """
    apply_current(current) sets the charger: 0 = stop, else charge with current A, returns success.
    ombc_allowed() tells if the CEM may control the charger (driver in Auto mode).
    """

    def __init__(self, item, apply_current, ombc_allowed, name, serial, model, firmware):
        self._item = item
        item.rm = self
        self._apply_current = apply_current
        self._ombc_allowed = ombc_allowed
        self._name = name
        self._serial = serial
        self._model = model
        self._firmware = firmware
        self._resource_id = uuid.uuid5(uuid.NAMESPACE_URL, 'go-eCharger/%s' % serial)
        self._parser = S2Parser()

        self.client_id = None
        self._keep_alive_interval = None
        self._last_seen = None
        self._watchdog = None

        self.control_type = None
        self._sysdesc = None
        self._sysdesc_id = None
        self._sysdesc_acked = False
        self._mode_current = {}  # operation mode id -> current, 0 = standby
        self._reported_mode_id = None

        self.max_current = 16
        self.phases = PHASES  # phases the car charges on
        self.requested_current = None  # last instruction of the CEM

        item.local_set_value('Ready')

    # --- D-Bus connection ---------------------------------------------------------------------

    @property
    def connected(self):
        return self.client_id is not None

    @property
    def ombc_active(self):
        return self.connected and self.control_type == ControlType.OPERATION_MODE_BASED_CONTROL

    def on_connect(self, client_id, keep_alive_interval):
        if self.connected:
            # a reconnect of the same client restarts the session
            if client_id != self.client_id:
                return False
            self._drop('reconnect', notify=False)

        logger.info("S2: %s connected, keep alive %d s", client_id, keep_alive_interval)
        self.client_id = client_id
        self._keep_alive_interval = max(1, keep_alive_interval)
        self._last_seen = time.time()
        self._watchdog = GLib.timeout_add_seconds(self._keep_alive_interval, self._check_keep_alive)
        self._item.local_set_value('CEM: %s' % client_id)
        # the RM starts the handshake, after the Connect call returned
        GLib.idle_add(self._send, Handshake(message_id=uuid.uuid4(), role=EnergyManagementRole.RM,
                                            supported_protocol_versions=[S2_VERSION]))
        return True

    def on_disconnect(self, client_id):
        if client_id == self.client_id:
            self._drop('client requested disconnect', notify=False)

    def on_keep_alive(self, client_id):
        if client_id != self.client_id:
            self._item.emit('Disconnect', client_id, 'Not connected')
            return False
        self._last_seen = time.time()
        return True

    def on_message(self, client_id, message):
        if client_id != self.client_id:
            self._item.emit('Disconnect', client_id, 'Not connected')
            return
        # answer outside of the method call
        GLib.idle_add(self._handle, message)

    def _check_keep_alive(self):
        if not self.connected:
            return False
        if time.time() - self._last_seen > 1.5 * self._keep_alive_interval:
            logger.warning("S2: %s missed the keep alive", self.client_id)
            self._drop('KeepAlive missed')
            return False
        return True

    def _drop(self, reason, notify=True):
        if not self.connected:
            return
        logger.info("S2: disconnecting %s: %s", self.client_id, reason)
        if notify:
            self._item.emit('Disconnect', self.client_id, reason)
        if self._watchdog is not None:
            GLib.source_remove(self._watchdog)
            self._watchdog = None
        self.client_id = None
        self.control_type = None
        self._sysdesc_acked = False
        self._reported_mode_id = None
        self._item.local_set_value('Ready')

    def _send(self, message):
        if self.connected:
            logger.debug("S2 OUT: %s", message.to_json())
            self._item.emit('Message', self.client_id, message.to_json())
        return False

    def _reply(self, subject, status=ReceptionStatusValues.OK, label=None):
        self._send(ReceptionStatus(subject_message_id=subject, status=status, diagnostic_label=label))

    # --- S2 messages from the CEM -------------------------------------------------------------

    def _handle(self, message):
        if not self.connected:
            return False
        logger.debug("S2 IN:  %s", message)
        try:
            msg = self._parser.parse_as_any_message(message)
        except (S2ValidationError, ValueError) as e:
            mid = None
            try:
                mid = json.loads(message).get('message_id')
            except ValueError:
                pass
            if mid:
                self._reply(mid, ReceptionStatusValues.INVALID_MESSAGE, str(e))
            return False

        try:
            if isinstance(msg, ReceptionStatus):
                if msg.status != ReceptionStatusValues.OK:
                    logger.warning("S2: CEM reports %s for %s: %s", msg.status, msg.subject_message_id, msg.diagnostic_label)
                elif self._sysdesc_id is not None and str(msg.subject_message_id) == str(self._sysdesc_id):
                    self._sysdesc_acked = True
                    self.report_status(force=True)

            elif isinstance(msg, Handshake):
                self._reply(msg.message_id)

            elif isinstance(msg, HandshakeResponse):
                self._reply(msg.message_id)
                self.send_details()

            elif isinstance(msg, SelectControlType):
                self._reply(msg.message_id)
                self.control_type = msg.control_type
                logger.info("S2: control type %s", msg.control_type)
                if self.ombc_active:
                    self.send_system_description()

            elif isinstance(msg, OMBCInstruction):
                current = self._mode_current.get(str(msg.operation_mode_id))
                if not self.ombc_active:
                    self._reply(msg.message_id, ReceptionStatusValues.TEMPORARY_ERROR, "Not in OMBC control")
                elif current is None:
                    self._reply(msg.message_id, ReceptionStatusValues.INVALID_CONTENT, "Unknown operation mode")
                else:
                    self._reply(msg.message_id)
                    logger.info("S2: instruction %d A%s", current, " (abnormal condition)" if msg.abnormal_condition else "")
                    self.requested_current = current
                    if self._apply_current(current):
                        self.report_status()

            else:
                self._reply(msg.message_id, ReceptionStatusValues.PERMANENT_ERROR,
                            "%s not supported" % type(msg).__name__)
        except Exception:
            logger.exception("S2: error handling %s", message)
        return False

    # --- S2 messages to the CEM ---------------------------------------------------------------

    def send_details(self):
        types = [ControlType.NOT_CONTROLABLE]
        if self._ombc_allowed():
            types.append(ControlType.OPERATION_MODE_BASED_CONTROL)
        self._send(ResourceManagerDetails(
            message_id=uuid.uuid4(),
            resource_id=self._resource_id,
            name=self._name,
            manufacturer='go-e',
            model=self._model,
            serial_number=self._serial,
            firmware_version=self._firmware,
            roles=[Role(role=RoleType.ENERGY_CONSUMER, commodity=Commodity.ELECTRICITY)],
            instruction_processing_delay=Duration.from_milliseconds(2000),
            available_control_types=types,
            provides_forecast=False,
            provides_power_measurement_types=list(PHASES),
        ))

    def send_system_description(self):
        modes = []
        self._mode_current = {}

        def mode(current):
            m = OMBCOperationMode(
                id=uuid.uuid4(),
                diagnostic_label='Standby' if current == 0 else '%d A %dP' % (current, len(self.phases)),
                power_ranges=[PowerRange(start_of_range=current * NOMINAL_VOLTAGE, end_of_range=current * NOMINAL_VOLTAGE,
                                         commodity_quantity=p) for p in self.phases],
                abnormal_condition_only=False,
            )
            modes.append(m)
            self._mode_current[str(m.id)] = current
            return m

        standby = mode(0)
        charging = [mode(c) for c in range(MIN_CURRENT, self.max_current + 1)]

        t_on = Timer(id=uuid.uuid4(), diagnostic_label='Minimum on time', duration=Duration.from_milliseconds(ON_MIN_S * 1000))
        t_off = Timer(id=uuid.uuid4(), diagnostic_label='Minimum off time', duration=Duration.from_milliseconds(OFF_MIN_S * 1000))
        t_step = Timer(id=uuid.uuid4(), diagnostic_label='Current step', duration=Duration.from_milliseconds(STEP_MIN_S * 1000))

        transitions = []

        def transition(frm, to, start, blocking, duration_s=0, abnormal=False):
            transitions.append(Transition(
                id=uuid.uuid4(), from_=frm.id, to=to.id,
                start_timers=[t.id for t in start], blocking_timers=[t.id for t in blocking],
                transition_duration=Duration.from_milliseconds(duration_s * 1000) if duration_s else None,
                abnormal_condition_only=abnormal,
            ))

        for m in charging:
            transition(standby, m, [t_on, t_step], [t_off], duration_s=10)
            transition(m, standby, [t_off], [t_on, t_step])
            transition(m, standby, [t_off], [], abnormal=True)
            for other in charging:
                if other is not m:
                    transition(m, other, [t_step], [t_step], duration_s=5)

        self._sysdesc = OMBCSystemDescription(
            message_id=uuid.uuid4(),
            valid_from=datetime.now(timezone.utc),
            operation_modes=modes,
            transitions=transitions,
            timers=[t_on, t_off, t_step],
        )
        self._sysdesc_id = self._sysdesc.message_id
        self._sysdesc_acked = False
        self._reported_mode_id = None
        logger.info("S2: system description, %d-%d A on %d phases", MIN_CURRENT, self.max_current, len(self.phases))
        self._send(self._sysdesc)

    def report_status(self, current=None, force=False):
        """Report the operation mode the charger is in: current 0 = standby, None = last instruction."""
        if not (self.ombc_active and self._sysdesc_acked):
            return
        if current is None:
            current = self.requested_current if self.requested_current is not None else 0
        if current:
            current = max(MIN_CURRENT, min(current, self.max_current))
        mode_id = next((i for i, c in self._mode_current.items() if c == current), None)
        if mode_id is None or (mode_id == self._reported_mode_id and not force):
            return
        self._send(OMBCStatus(message_id=uuid.uuid4(), active_operation_mode_id=mode_id, operation_mode_factor=1.0))
        self._reported_mode_id = mode_id

    def report_power(self, l1, l2, l3):
        if not self.connected or self.control_type is None:
            return
        self._send(PowerMeasurement(
            message_id=uuid.uuid4(),
            measurement_timestamp=datetime.now(timezone.utc),
            values=[PowerValue(commodity_quantity=p, value=float(v)) for p, v in zip(PHASES, (l1, l2, l3))],
        ))

    # --- changes on the driver side -----------------------------------------------------------

    def update_capabilities(self, max_current=None, phases=None):
        """Charger maximum or car phases changed: a new system description is needed."""
        changed = False
        if max_current is not None and max_current >= MIN_CURRENT and max_current != self.max_current:
            self.max_current = max_current
            changed = True
        if phases and tuple(phases) != tuple(self.phases):
            self.phases = tuple(phases)
            changed = True
        if changed and self.ombc_active:
            self.send_system_description()

    def mode_changed(self):
        """The driver mode changed, offer the control types again."""
        if not self.ombc_allowed_now():
            self.requested_current = None
        if self.connected:
            self.send_details()

    def ombc_allowed_now(self):
        return self._ombc_allowed()
