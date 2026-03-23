from __future__ import annotations

import time
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .mqtt_client import EcoFlowMqttClient

# Device type constants
DELTA_PRO = "delta_pro"
SMART_HOME_PANEL = "smart_home_panel"

DEVICE_TYPE_LABELS = {
    DELTA_PRO: "Delta Pro",
    SMART_HOME_PANEL: "Smart Home Panel",
}


def _fmt_version(v: float) -> str:
    """Decode EcoFlow packed firmware version integer to X.X.X.X string."""
    n = int(v)
    if n <= 0:
        return "--"
    return f"{(n >> 24) & 0xFF}.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"


def _wifi_icon(rssi: float) -> str:
    """Return WiFi signal description + color markup from RSSI value."""
    r = int(rssi)
    if r == 0:
        return "[dim]N/A[/]"
    # EcoFlow reports positive values — negate to get standard dBm
    if r > 0:
        r = -r
    if r >= -50:
        return f"[green]{r} dBm (Excellent)[/]"
    if r >= -60:
        return f"[green]{r} dBm (Good)[/]"
    if r >= -70:
        return f"[yellow]{r} dBm (Fair)[/]"
    return f"[red]{r} dBm (Weak)[/]"


def _soc_color(soc: float) -> str:
    if soc >= 60:
        return "green"
    if soc >= 20:
        return "yellow"
    return "red"


def _soh_color(soh: float) -> str:
    if soh >= 80:
        return "green"
    if soh >= 60:
        return "yellow"
    return "red"


def _soh_label(soh: float) -> str:
    if soh >= 90:
        return "Excellent"
    if soh >= 80:
        return "Good"
    if soh >= 60:
        return "Fair"
    return "Poor"


def _fmt_time(minutes: float) -> str:
    m = int(minutes)
    if m <= 0:
        return "--"
    if m >= 1440:
        return f"{m // 1440}d {(m % 1440) // 60}h"
    return f"{m // 60}h {m % 60}m"


def _fmt_watts(w: float) -> str:
    if abs(w) >= 1000:
        return f"{w / 1000:.1f} kW"
    return f"{int(w)} W"


def _fmt_wh(wh: float) -> str:
    if wh >= 1000:
        return f"{wh / 1000:.2f} kWh"
    return f"{int(wh)} Wh"


def _get(data: dict, *keys: str, default: float = 0) -> float:
    """Try multiple keys, return first non-zero value found."""
    for key in keys:
        v = data.get(key)
        if v is not None:
            try:
                fv = float(v)
                if fv != 0:
                    return fv
            except (TypeError, ValueError):
                continue
    # All were zero or missing — return first valid value or default
    for key in keys:
        v = data.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _build_delta_pro_panel(sn: str, data: dict, name: str, device_type: str = DELTA_PRO) -> Panel:
    type_label = DEVICE_TYPE_LABELS.get(device_type, device_type)
    soc = _get(data, "ems.lcdShowSoc", "ems.f32LcdShowSoc", "bmsMaster.f32ShowSoc", "bmsMaster.soc")
    soh = _get(data, "bmsMaster.soh")
    color = _soc_color(soc)

    # Capacity in Wh: remainCap (mAh) * vol (mV) / 1e6
    remain_cap_mah = _get(data, "bmsMaster.remainCap")
    full_cap_mah = _get(data, "bmsMaster.fullCap")
    design_cap_mah = _get(data, "bmsMaster.designCap")
    volts_mv = _get(data, "bmsMaster.vol")
    remain_wh = remain_cap_mah * volts_mv / 1e6 if remain_cap_mah and volts_mv else 0
    full_wh = full_cap_mah * volts_mv / 1e6 if full_cap_mah and volts_mv else 0

    # WiFi and firmware
    wifi_rssi = _get(data, "pd.wifiRssi")
    fw_pd = _get(data, "pd.sysVer")
    fw_bms = _get(data, "bmsMaster.sysVer")
    fw_inv = _get(data, "inv.sysVer")
    fw_mppt = _get(data, "mppt.swVer")

    # Battery header: large SOC + health + capacity
    bar = ProgressBar(total=100, completed=max(0, min(100, soc)), style=color, complete_style=color)

    soh_c = _soh_color(soh)
    soh_lbl = _soh_label(soh)

    # Title with device type + SOC prominently displayed
    panel_title = (
        f"[bold]{type_label}[/bold] [dim]({sn})[/dim]"
        f"  [{color} bold]{int(soc)}%[/]"
    )
    # Subtitle with health and capacity
    panel_subtitle = (
        f"Health: [{soh_c} bold]{int(soh)}%[/] [{soh_c}]({soh_lbl})[/]"
        f"  [dim]{_fmt_wh(remain_wh)} / {_fmt_wh(full_wh)}[/]"
    )

    # Power I/O table
    t = Table.grid(padding=(0, 2))
    t.add_column(min_width=16)
    t.add_column(min_width=10, justify="right")
    t.add_column(min_width=16)
    t.add_column(min_width=10, justify="right")

    # Power inputs
    solar_in = _get(data, "mppt.inWatts")
    ac_in = _get(data, "inv.inputWatts")
    total_in = _get(data, "pd.wattsInSum")
    total_out = _get(data, "pd.wattsOutSum")

    # Solar details
    solar_vol = _get(data, "mppt.inVol")
    solar_amp = _get(data, "mppt.inAmp")
    # Auto-detect mV vs V: if >500, it's likely mV
    if solar_vol > 500:
        solar_vol /= 1000
    if solar_amp > 100:
        solar_amp /= 1000

    # AC details
    ac_in_vol = _get(data, "inv.acInVol") / 1000 if _get(data, "inv.acInVol") > 100 else _get(data, "inv.acInVol")
    ac_in_freq = _get(data, "inv.acInFreq")
    ac_out_vol = _get(data, "inv.invOutVol") / 1000 if _get(data, "inv.invOutVol") > 100 else _get(data, "inv.invOutVol")
    ac_out_freq = _get(data, "inv.invOutFreq")

    # Power outputs — individual loads
    ac_out = _get(data, "inv.outputWatts")
    car = _get(data, "pd.carWatts")
    usb1 = _get(data, "pd.usb1Watts")
    usb2 = _get(data, "pd.usb2Watts")
    typec1 = _get(data, "pd.typec1Watts")
    typec2 = _get(data, "pd.typec2Watts")
    usb_total = usb1 + usb2 + typec1 + typec2

    # Solar row with inline voltage/current
    solar_detail = ""
    if solar_in and solar_vol:
        solar_detail = f" [dim]({solar_vol:.1f}V {solar_amp:.1f}A)[/]"
    t.add_row(
        Text("Solar In", style="dim"),
        Text.from_markup(f"[green]{_fmt_watts(solar_in)}[/]{solar_detail}") if solar_in else Text(_fmt_watts(0), style="dim"),
        Text("AC In", style="dim"),
        Text.from_markup(f"[green]{_fmt_watts(ac_in)}[/] [dim]({ac_in_vol:.0f}V {ac_in_freq:.0f}Hz)[/]") if ac_in else Text(_fmt_watts(0), style="dim"),
    )

    # AC out with inline voltage/freq
    ac_out_detail = ""
    if ac_out and ac_out_vol:
        ac_out_detail = f" [dim]({ac_out_vol:.0f}V {ac_out_freq:.0f}Hz)[/]"
    t.add_row(
        Text("AC Out", style="dim"),
        Text.from_markup(f"[red]{_fmt_watts(ac_out)}[/]{ac_out_detail}") if ac_out else Text(_fmt_watts(0), style="dim"),
        Text("12V/Car Out", style="dim"), Text(_fmt_watts(car), style="red" if car else "dim"),
    )
    if usb_total:
        t.add_row(
            Text("USB Out", style="dim"), Text(_fmt_watts(usb_total), style="red"),
            Text("", style="dim"), Text(""),
        )

    # Efficiency: mppt.outWatts is DC bus power, total_out is user-facing output
    mppt_out = _get(data, "mppt.outWatts")
    eff_str = ""
    if mppt_out > 0 and total_out > 0:
        eff = total_out / mppt_out * 100
        eff_color = "green" if eff >= 90 else "yellow" if eff >= 80 else "red"
        eff_str = f"  [{eff_color}]({eff:.0f}% eff)[/]"

    t.add_row(
        Text("Total In", style="bold dim"), Text(_fmt_watts(total_in), style="bold green" if total_in else "dim"),
        Text("Total Out", style="bold dim"),
        Text.from_markup(f"[bold red]{_fmt_watts(total_out)}[/]{eff_str}") if total_out else Text(_fmt_watts(0), style="dim"),
    )

    # Charge / Discharge time — use power flow to decide, not just timers
    chg = _get(data, "ems.chgRemainTime")
    dsg = _get(data, "ems.dsgRemainTime")
    is_charging = total_in > total_out and total_in > 0
    if is_charging:
        time_label = "Charge"
        time_val = _fmt_time(chg)
    elif total_out > 0:
        time_label = "Discharge"
        time_val = _fmt_time(dsg)
    else:
        time_label = "Idle"
        time_val = "--"

    volts = volts_mv / 1000 if volts_mv > 100 else volts_mv
    cycles = int(_get(data, "bmsMaster.cycles"))
    min_dsg = int(_get(data, "ems.minDsgSoc"))
    max_chg = int(_get(data, "ems.maxChargeSoc"))

    # Battery detail fields
    batt_in = _get(data, "bmsMaster.inputWatts")
    batt_out = _get(data, "bmsMaster.outputWatts")
    batt_amp = _get(data, "bmsMaster.amp")
    if abs(batt_amp) > 100:
        batt_amp /= 1000  # mA → A
    min_cell_v = _get(data, "bmsMaster.minCellVol")
    max_cell_v = _get(data, "bmsMaster.maxCellVol")
    if min_cell_v > 100:
        min_cell_v /= 1000  # mV → V
    if max_cell_v > 100:
        max_cell_v /= 1000
    cell_delta = (max_cell_v - min_cell_v) * 1000  # mV delta
    delta_color = "green" if cell_delta <= 20 else "yellow" if cell_delta <= 50 else "red"

    # Temperatures
    batt_temp = _get(data, "bmsMaster.temp")
    min_cell_t = _get(data, "bmsMaster.minCellTemp")
    max_cell_t = _get(data, "bmsMaster.maxCellTemp")
    inv_temp = _get(data, "inv.outTemp")
    inv_dc_temp = _get(data, "inv.dcInTemp")
    mppt_temp = _get(data, "mppt.mpptTemp")
    mos_temp = _get(data, "bmsMaster.maxMosTemp")

    stats = Table.grid(padding=(0, 2))
    stats.add_column(min_width=16)
    stats.add_column(min_width=10, justify="right")
    stats.add_column(min_width=16)
    stats.add_column(min_width=10, justify="right")
    stats.add_row(
        Text(f"{time_label} Time", style="dim"), Text(time_val),
        Text("Voltage", style="dim"), Text(f"{volts:.1f} V"),
    )
    stats.add_row(
        Text("Current", style="dim"),
        Text(f"{batt_amp:+.1f} A", style="green" if batt_amp > 0 else "red" if batt_amp < 0 else "dim"),
        Text("DC Bus", style="dim"), Text(_fmt_watts(mppt_out), style="dim" if not mppt_out else ""),
    )
    stats.add_row(
        Text("Cell V", style="dim"),
        Text.from_markup(f"{min_cell_v:.2f}-{max_cell_v:.2f}V [{delta_color}]\u0394{cell_delta:.0f}mV[/]"),
        Text("Cell T", style="dim"), Text(f"{int(min_cell_t)} - {int(max_cell_t)}°C"),
    )
    stats.add_row(
        Text("Batt / Inv", style="dim"), Text(f"{int(batt_temp)}° / {int(inv_temp)}°C"),
        Text("MPPT / MOS", style="dim"), Text(f"{int(mppt_temp)}° / {int(mos_temp)}°C"),
    )

    # Fan status
    fan_level = _get(data, "ems.fanLevel")
    inv_fan = _get(data, "inv.fanState")
    fan_mode = _get(data, "pd.iconFanMode")  # 0=auto, 1-3=levels
    fan_parts = []
    if inv_fan:
        fan_parts.append("[yellow]Inv ON[/]")
    if fan_level:
        fan_parts.append(f"[yellow]EMS Lv{int(fan_level)}[/]")
    fan_txt = " ".join(fan_parts) if fan_parts else "[green]Off[/]"
    mode_txt = ["Auto", "Lv1", "Lv2", "Lv3"][int(fan_mode)] if fan_mode < 4 else "Auto"

    beep = _get(data, "pd.beepState")

    stats.add_row(
        Text("Cycles", style="dim"), Text(str(cycles)),
        Text("Limits", style="dim"), Text(f"{min_dsg}% - {max_chg}%"),
    )
    stats.add_row(
        Text("Fan", style="dim"), Text.from_markup(f"{fan_txt} [dim]({mode_txt})[/]"),
        Text("Beep", style="dim"), Text.from_markup("[green]ON[/]" if not beep else "[dim]OFF[/]"),
    )

    # Firmware info (+ WiFi if available)
    net_fw = Table.grid(padding=(0, 2))
    net_fw.add_column(min_width=16)
    net_fw.add_column(min_width=10, justify="right")
    net_fw.add_column(min_width=16)
    net_fw.add_column(min_width=10, justify="right")
    if wifi_rssi:
        net_fw.add_row(
            Text("WiFi", style="dim"), Text.from_markup(_wifi_icon(wifi_rssi)),
            Text("PD Firmware", style="dim"), Text(_fmt_version(fw_pd), style="dim"),
        )
    else:
        net_fw.add_row(
            Text("PD Firmware", style="dim"), Text(_fmt_version(fw_pd), style="dim"),
            Text("MPPT FW", style="dim"), Text(_fmt_version(fw_mppt), style="dim"),
        )
    net_fw.add_row(
        Text("BMS Firmware", style="dim"), Text(_fmt_version(fw_bms), style="dim"),
        Text("Inverter FW", style="dim"), Text(_fmt_version(fw_inv), style="dim"),
    )

    # Lifetime energy stats
    chg_ac = _get(data, "pd.chgPowerAc")
    chg_sun = _get(data, "pd.chgSunPower")
    dsg_ac = _get(data, "pd.dsgPowerAc")
    dsg_dc = _get(data, "pd.dsgPowerDc")
    if any([chg_ac, chg_sun, dsg_ac, dsg_dc]):
        lt = Table.grid(padding=(0, 2))
        lt.add_column(min_width=16)
        lt.add_column(min_width=10, justify="right")
        lt.add_column(min_width=16)
        lt.add_column(min_width=10, justify="right")
        lt.add_row(
            Text("AC Charged", style="dim"), Text(_fmt_wh(chg_ac), style="green"),
            Text("Solar Charged", style="dim"), Text(_fmt_wh(chg_sun), style="green"),
        )
        lt.add_row(
            Text("AC Discharged", style="dim"), Text(_fmt_wh(dsg_ac), style="red"),
            Text("DC Discharged", style="dim"), Text(_fmt_wh(dsg_dc), style="red"),
        )
        return Panel(
            Group(bar, "", t, "", stats, "", net_fw, "", Text.from_markup("[dim]Lifetime Energy[/]"), lt),
            title=panel_title,
            subtitle=panel_subtitle,
            border_style="blue",
        )

    return Panel(
        Group(bar, "", t, "", stats, "", net_fw),
        title=panel_title,
        subtitle=panel_subtitle,
        border_style="blue",
    )


def _shp_get_str(data: dict, *keys: str) -> str:
    """Get a string value trying multiple keys."""
    for key in keys:
        v = data.get(key)
        if v and isinstance(v, str):
            return v
    return ""


def _fmt_uptime(seconds: float) -> str:
    """Format uptime in seconds into Xd Xh."""
    s = int(seconds)
    if s <= 0:
        return "--"
    days = s // 86400
    hours = (s % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {(s % 3600) // 60}m"


# Delta Pro nominal voltage for mAh → Wh conversion
_NOMINAL_V = 51.2


def _mah_to_wh(mah: float) -> float:
    """Convert mAh to Wh using nominal battery voltage (51.2V for Delta Pro)."""
    if mah <= 0:
        return 0
    # SHP reports capacity in mAh; if value > 10000 it's definitely mAh
    if mah > 10000:
        return mah * _NOMINAL_V / 1000
    # Smaller values might already be Wh
    return mah


def _build_shp_panel(sn: str, data: dict, name: str, device_type: str = SMART_HOME_PANEL) -> Panel:
    type_label = DEVICE_TYPE_LABELS.get(device_type, device_type)

    # ── Grid + Energy (compact: 2 lines) ──
    grid_sta = _get(data, "gridSta", "heartbeat.gridSta")
    grid_vol = _get(data, "gridInfo.gridVol", "gridVol")
    grid_freq = _get(data, "gridInfo.gridFreq", "gridFreq")
    grid_day_wh = _get(data, "gridDayWatth", "heartbeat.gridDayWatth")
    backup_day_wh = _get(data, "backupDayWatth", "heartbeat.backupDayWatth")
    eps_mode = _get(data, "epsModeInfo.eps", "eps")
    self_check = _get(data, "selfCheck.result")
    self_check_err = _get(data, "selfCheck.errorCode")

    grid_v = f" ({grid_vol:.0f}V {grid_freq:.0f}Hz)" if grid_vol else ""
    eps_txt = " [yellow bold]EPS[/]" if eps_mode else ""
    chk_txt = ""
    if self_check == 0 and self_check_err:
        chk_txt = f" [red]Self-Check FAIL (err {int(self_check_err)})[/]"

    info = Table.grid(padding=(0, 2))
    info.add_column(min_width=14)
    info.add_column(min_width=10, justify="right")
    info.add_column(min_width=14)
    info.add_column(min_width=10, justify="right")
    info.add_column(min_width=14)
    info.add_column(min_width=10, justify="right")
    info.add_row(
        Text("Grid", style="dim"),
        Text.from_markup(f"{'[bold green]ON[/]' if grid_sta else '[bold red]OFF[/]'}{grid_v}{eps_txt}{chk_txt}"),
        Text("Grid Today", style="dim"), Text(_fmt_wh(grid_day_wh), style="bold"),
        Text("Backup Today", style="dim"), Text(_fmt_wh(backup_day_wh), style="bold"),
    )

    # ── Battery summary (compact: 2 lines) ──
    backup_bat_pct = _get(data, "backupBatPer", "heartbeat.backupBatPer")
    backup_full_cap = _get(data, "backupFullCap", "heartbeat.backupFullCap")
    backup_chg_time = _get(data, "backupChaTime", "heartbeat.backupChaTime")
    disc_lower = _get(data, "backupChaDiscCfg.discLower", "discLower")
    force_chg_high = _get(data, "backupChaDiscCfg.forceChargeHigh", "forceChargeHigh")
    sched_enabled = _get(data, "timeTask.cfg.comCfg.isEnable")
    sched_watt = _get(data, "timeTask.cfg.param.chChargeWatt")
    sched_target = _get(data, "timeTask.cfg.param.hightBattery")

    backup_full_cap_wh = _mah_to_wh(backup_full_cap)

    batt_color = _soc_color(backup_bat_pct)
    limits_txt = f"{int(disc_lower)}%–{int(force_chg_high)}%" if force_chg_high else "--"
    sched_txt = f"[green]ON[/] {_fmt_watts(sched_watt)}→{int(sched_target)}%" if sched_enabled else "[dim]OFF[/]"

    info.add_row(
        Text("Combined", style="dim"),
        Text.from_markup(f"[{batt_color} bold]{int(backup_bat_pct)}%[/]  {_fmt_wh(backup_full_cap_wh)}"),
        Text("Limits", style="dim"), Text(limits_txt),
        Text("Sched Chg", style="dim"), Text.from_markup(sched_txt),
    )

    # ── Per-battery detail (compact: 2 lines each when connected) ──
    batt_lines: list[Text | Table] = []
    for i in range(2):
        prefixes = [f"energyInfos.{i}", f"heartbeat.energyInfos.{i}"]

        def _b(field: str, _pfx=prefixes) -> float:
            for pfx in _pfx:
                v = _get(data, f"{pfx}.{field}")
                if v:
                    return v
            return 0.0

        connected = _b("stateBean.isConnect")
        if not connected:
            batt_lines.append(Text.from_markup(f"  [dim]Batt {i+1}: Not connected[/]"))
            continue

        soc = _b("batteryPercentage")
        bat_temp = _b("emsBatTemp")
        chg_time = _b("chargeTime")
        dsg_time = _b("dischargeTime")
        in_watts = _b("lcdInputWatts")
        out_watts = _b("outputPower")
        full_cap = _b("fullCap")
        rate_power = _b("ratePower")
        grid_chg = _b("stateBean.isGridCharge")
        is_output = _b("stateBean.isPowerOutput")
        is_ac_open = _b("stateBean.isAcOpen")
        is_mppt = _b("stateBean.isMpptCharge")

        color = _soc_color(soc)
        flags = []
        if grid_chg:
            flags.append("[green]GridChg[/]")
        if is_mppt:
            flags.append("[green]Solar[/]")
        if is_output:
            flags.append("[red]Output[/]")
        if is_ac_open:
            flags.append("[cyan]AC[/]")
        flags_str = " ".join(flags) if flags else "[dim]Standby[/]"

        time_str = ""
        if chg_time > 0:
            time_str = f"Chg:{_fmt_time(chg_time)}"
        elif dsg_time > 0:
            time_str = f"Dsg:{_fmt_time(dsg_time)}"

        pwr = ""
        if in_watts:
            pwr += f" [green]+{_fmt_watts(in_watts)}[/]"
        if out_watts:
            pwr += f" [red]-{_fmt_watts(out_watts)}[/]"

        full_cap_wh = _mah_to_wh(full_cap)
        batt_lines.append(Text.from_markup(
            f"  Batt {i+1}: [{color} bold]{int(soc)}%[/] {int(bat_temp)}°C"
            f"  {flags_str}{pwr}"
            f"  [dim]{_fmt_wh(full_cap_wh)} / {_fmt_watts(rate_power)}[/]"
            f"  {time_str}"
        ))

    # ── Circuits (2-column, 6 rows) ──
    # Collect all circuit data first
    circuit_data = []
    total_load = 0.0
    total_amps = 0.0
    for i in range(12):
        w = _get(data,
                 f"infoList.{i}.chWatt",
                 f"loadCmdChCtrlInfos.{i}.ctrlWatt",
                 f"heartbeat.loadCmdChCtrlInfos.{i}.ctrlWatt")
        cur = _get(data,
                   f"loadChCurInfo.cur.{i}",
                   f"heartbeat.loadChCurInfo.cur.{i}")
        mode = _get(data,
                    f"loadCmdChCtrlInfos.{i}.ctrlMode",
                    f"heartbeat.loadCmdChCtrlInfos.{i}.ctrlMode")
        priority = _get(data,
                        f"loadCmdChCtrlInfos.{i}.priority",
                        f"heartbeat.loadCmdChCtrlInfos.{i}.priority",
                        f"emergencyStrategy.chSta.{i}.priority")
        ch_name = ""
        for key in [
            f"loadChInfo.info.{i}.chName",
            f"info.{i}.chName",
            f"heartbeat.loadChInfo.info.{i}.chName",
        ]:
            v = data.get(key)
            if v and isinstance(v, str):
                ch_name = v
                break

        circuit_data.append((i, w, cur, mode, priority, ch_name))
        total_load += w
        total_amps += cur

    # 2-column circuit table: left=circuits 1-6, right=circuits 7-12
    ct = Table(show_header=True, show_edge=False, pad_edge=False, title="Circuits")
    ct.add_column("#", style="dim", width=2)
    ct.add_column("Name", width=8)
    ct.add_column("Power", justify="right", width=7)
    ct.add_column("Amps", justify="right", width=5)
    ct.add_column("Mode", width=4)
    ct.add_column("Pri", justify="center", width=3)
    ct.add_column("│", style="dim", width=1)
    ct.add_column("#", style="dim", width=2)
    ct.add_column("Name", width=8)
    ct.add_column("Power", justify="right", width=7)
    ct.add_column("Amps", justify="right", width=5)
    ct.add_column("Mode", width=4)
    ct.add_column("Pri", justify="center", width=3)

    for row_idx in range(6):
        row = []
        for j, col in enumerate([row_idx, row_idx + 6]):
            if j > 0:
                row.append("│")
            idx, w, cur, mode, priority, ch_name = circuit_data[col]
            w_style = "bold" if w > 100 else "" if w > 0 else "dim"
            mode_txt = "A" if mode == 0 else "M" if mode == 1 else "-"
            pri_txt = str(int(priority)) if priority else "-"
            cur_txt = f"{cur:.1f}" if cur else "--"
            row.extend([
                str(idx + 1),
                Text(ch_name[:8] if ch_name else "", style="dim"),
                Text(_fmt_watts(w), style=w_style),
                Text(cur_txt, style="dim" if not cur else ""),
                Text(mode_txt, style="dim"),
                Text(pri_txt, style="dim"),
            ])
        ct.add_row(*row)

    total_a_txt = f" ({total_amps:.1f}A)" if total_amps else ""

    # ── System footer ──
    emerg_backup = _get(data, "emergencyStrategy.backupMode")
    emerg_overload = _get(data, "emergencyStrategy.overloadMode")
    work_time_raw = _get(data, "heartbeat.workTime", "workTime")
    # SHP reports workTime in milliseconds (confirmed: increases by ~1000/sec)
    work_time = work_time_raw / 1000 if work_time_raw > 0 else 0
    sys_parts = []
    if emerg_backup or emerg_overload:
        sys_parts.append(f"Emerg: Bkup={int(emerg_backup)} Overld={int(emerg_overload)}")
    if work_time:
        sys_parts.append(f"Up: {_fmt_uptime(work_time)}")

    # ── Assemble ──
    elements: list = [info, ""]
    elements.extend(batt_lines)
    elements.extend(["", ct])

    subtitle_parts = [f"[bold]{_fmt_watts(total_load)}[/]{total_a_txt}"]
    if sys_parts:
        subtitle_parts.append(" | ".join(sys_parts))

    return Panel(
        Group(*elements),
        title=f"[bold]{type_label}[/bold] [dim]({sn})[/dim]",
        subtitle=" | ".join(subtitle_parts),
        border_style="cyan",
    )


def _build_help_bar(
    commands: dict,
    selected_sn: str,
    selected_idx: int,
    device_count: int,
    status_msg: str,
) -> Group:
    """Build the bottom help bar with available shortcuts."""
    from .controls import KEY_LABELS

    # Command shortcuts
    parts = []
    for key, cmd in commands.items():
        label = KEY_LABELS.get(key, key)
        parts.append(f"[bold cyan]{label}[/] {cmd.label}")

    # Device selector
    dev_sel = f"  [bold yellow]1-{device_count}[/] Device"
    help_line = "  ".join(parts) + dev_sel + "  [bold yellow]h[/] Help  [dim]Ctrl+C Exit[/]"

    elements = []
    if status_msg:
        elements.append(Text.from_markup(f"  [bold green]{status_msg}[/]"))
    elements.append(Text.from_markup(f"  [dim]Device {selected_idx + 1}[/] {selected_sn}"))
    elements.append(Text.from_markup(f"  {help_line}"))
    return Group(*elements)


def build_dashboard(
    mqtt_client: EcoFlowMqttClient,
    device_types: dict[str, str],
    device_names: dict[str, str],
    selected_sn: str = "",
    status_msg: str = "",
    commands: dict | None = None,
) -> Group:
    panels = []

    status = "[green]MQTT Connected[/]" if mqtt_client.connected else "[red]MQTT Disconnected[/]"
    header = Text.from_markup(
        f"[bold]EcoFlow Dashboard[/]  {status}  "
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/]"
    )
    panels.append(header)
    panels.append(Text(""))

    sns = list(device_types.keys())
    selected_idx = sns.index(selected_sn) if selected_sn in sns else 0

    # Collect panels by type
    delta_panels = []
    other_panels = []
    for sn, dtype in device_types.items():
        data = mqtt_client.get_device_data(sn)
        name = device_names.get(sn, sn)
        if dtype == DELTA_PRO:
            delta_panels.append(_build_delta_pro_panel(sn, data, name, dtype))
        elif dtype == SMART_HOME_PANEL:
            other_panels.append(_build_shp_panel(sn, data, name, dtype))
        else:
            other_panels.append(Panel(f"Unknown device type: {dtype}", title=sn))

    # Place Delta Pros side by side if there are exactly 2
    if len(delta_panels) == 2:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(delta_panels[0], delta_panels[1])
        panels.append(grid)
    else:
        panels.extend(delta_panels)

    panels.extend(other_panels)

    # Help bar
    if commands is not None:
        panels.append(_build_help_bar(
            commands, selected_sn, selected_idx, len(sns), status_msg,
        ))
    else:
        panels.append(Text.from_markup("\n[dim]Press Ctrl+C to exit[/]"))

    return Group(*panels)


def run_dashboard(
    mqtt_client: EcoFlowMqttClient,
    device_types: dict[str, str],
    device_names: dict[str, str],
) -> None:
    import queue as _queue
    from .controls import DeviceController, KeyboardThread

    sns = list(device_types.keys())
    selected_sn = sns[0]

    controller = DeviceController(mqtt_client, device_types)
    key_queue: _queue.Queue = _queue.Queue()
    kb_thread = KeyboardThread(key_queue)
    kb_thread.start()

    status_msg = ""
    status_ttl = 0  # ticks remaining to show status

    try:
        with Live(
            build_dashboard(
                mqtt_client, device_types, device_names,
                selected_sn=selected_sn,
                commands=controller.get_commands(selected_sn),
            ),
            refresh_per_second=2,
            screen=True,
        ) as live:
            while True:
                # Process pending keystrokes
                while not key_queue.empty():
                    key = key_queue.get_nowait()

                    # Device selector: 1-9
                    if key.isdigit() and 1 <= int(key) <= len(sns):
                        selected_sn = sns[int(key) - 1]
                        status_msg = f"Selected: {selected_sn}"
                        status_ttl = 4
                    else:
                        result = controller.handle_key(key, selected_sn)
                        if result:
                            status_msg = result
                            status_ttl = 6

                # Fade status message
                if status_ttl > 0:
                    status_ttl -= 1
                else:
                    status_msg = ""

                live.update(build_dashboard(
                    mqtt_client, device_types, device_names,
                    selected_sn=selected_sn,
                    status_msg=status_msg,
                    commands=controller.get_commands(selected_sn),
                ))
                time.sleep(0.5)
    finally:
        kb_thread.stop()
