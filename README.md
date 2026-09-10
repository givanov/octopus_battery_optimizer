# Octopus Battery Optimizer (Home Assistant / HACS)

A Home Assistant custom integration that automatically charges and discharges a
DIY battery based on **Octopus Agile** electricity prices.

It watches the published hourly/half-hourly prices and:

- **Charges** the battery during the **cheapest `charge_hours` block** of the day.
- **Discharges** the battery (powering your load) during the **most expensive `use_hours` block** of the day.
- Keeps the battery **topped up** in between, and performs a small **maintenance top-up** so it never sits completely flat.

It drives two switches:

| Switch | Meaning |
|--------|---------|
| **Battery switch** (smart plug on the battery charger) | `ON` = battery is charging from the mains, `OFF` = not charging |
| **Shelly switch** (Shelly 1 Gen4 contactor) | `ON` = load is fed from the **mains**, `OFF` = load is fed from the **battery** |

---

## How it works

Every `check_interval` minutes (and immediately whenever new prices are fetched)
the integration evaluates the current situation and picks a **mode**:

| Mode | Battery plug | Shelly (mains) | When |
|------|:-----------:|:--------------:|------|
| `charging` | ON | ON | Inside the cheapest block, while SoC < charge target |
| `discharging` | OFF | OFF | Inside the most expensive block, while SoC > stop threshold |
| `top_up` | ON | ON | Outside both blocks, SoC dropped below the top-up trigger |
| `idle` | OFF | ON | Everything else (load stays on the mains, battery rests) |

### The rules

1. **Charge block** – the cheapest `charge_hours` contiguous block of published
   prices that starts today. The battery charges to the *charge target* (default
   100 %).
2. **Use block** – the most expensive `use_hours` contiguous block that starts
   today. The battery discharges down to the *discharge stop* SoC (default 5 %)
   or until the block ends, whichever comes first.
3. **Topped up** – once charged, the battery stays full (load on the mains)
   until the use block arrives.
4. **Maintenance top-up** – when the battery falls below the *top-up trigger*
   (default 3 %), it is topped back up to the *top-up target* (default 5 %).
   This hysteresis band prevents constant on/off cycling.

> **Note on blocks:** both blocks are anchored to the *current calendar day*, so
> the schedule is stable all day (it never abandons a block it is already in).
> A block may run past midnight (e.g. 23:00 → 03:00). The current Agile tariff
> is priced **half-hourly**; block selection is duration-based, so it works with
> both hourly and half-hourly data.

---

## Installation (HACS)

1. In HACS, go to **Integrations** → **⋮** → **Custom Repositories** and add:
   `https://github.com/givanov/battery_charger`
2. Search for **Octopus Battery Optimizer** and click **Download**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and choose
   **Octopus Battery Optimizer**.

> Requires Home Assistant **2024.5.0** or newer. No API key is needed – the
> price endpoint is public.

---

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| **Name** | `Octopus Battery` | Friendly name for the integration / device |
| **Battery charging switch** | – | The smart plug feeding the battery charger (`switch.*`) |
| **Shelly contactor switch** | – | The Shelly 1 Gen4 that selects mains (`ON`) vs battery (`OFF`) (`switch.*`) |
| **Battery SoC sensor** | – | A numeric sensor reporting state of charge 0–100 % (`sensor.*`) |
| **Discharge block length** | `4` | `use_hours` – the most expensive N hours to run on battery |
| **Charge block length** | `4` | `charge_hours` – the cheapest N hours to charge in |
| **Octopus product code** | `AGILE-24-10-01` | Agile product code |
| **Octopus tariff code** | `E-1R-AGILE-24-10-01-A` | Agile tariff code (see note below) |
| **Stop discharging below** | `5` | % – discharge down to this SoC, then switch back to mains |
| **Top-up trigger below** | `3` | % – start a maintenance top-up when SoC falls below this |
| **Top-up target** | `5` | % – top the battery back up to this SoC |
| **Charge target** | `100` | % – how full to charge during the cheap block |
| **Evaluation interval** | `5` | minutes between re-evaluations |

> **Tariff code note:** the defaults target the current *Agile Octopus* tariff.
> If your account shows a different product/tariff code (Octopus changes these
> over time), enter your own – you can find them in the Octopus app or via
> `https://api.octopus.energy/v1/products`. The unit rates are the same across
> payment-method variants, so any `E-1R-AGILE-…` variant will work.

All of the above can be changed later via **Settings → Devices & Services →
Octopus Battery Optimizer → ⋮ → Configure**. The **Dry run mode** is *not* an
option – it is a live switch on the device (see below) that you toggle directly.

---

## Dry run mode

A **Dry run mode** switch is exposed on the device. When it is **ON**, the
integration keeps doing all the thinking – it still fetches prices, works out
the cheapest/most-expensive blocks, and reports the would-be mode in the
sensors – but it **does not change either physical switch**. This is useful
when you want to see what the integration *would* do (or drive the battery
manually) without it fighting you for the switches.

The setting **persists** across restarts: toggling the switch writes the value
into the config entry, so it is the single source of truth (it is deliberately
kept out of the options flow so the two can't disagree).

Turn it back **OFF** to resume automatic control; the current mode's switch
states are re-applied at that point.

---

## Sensors

The integration exposes a set of diagnostic sensors (grouped under one device):

| Sensor | Description |
|--------|-------------|
| **Mode** | Current mode: `idle`, `charging`, `discharging`, `top_up` (plus `dry_run`, `override_active`, `top_up_in_progress`, `last_error` attributes) |
| **Use block start / end** | Start & end (HH:MM) of today's most-expensive block |
| **Use block price** | Total price (p/kWh) of the use block |
| **Charge block start / end** | Start & end (HH:MM) of today's cheapest block |
| **Charge block price** | Total price (p/kWh) of the charge block |
| **Battery level** | Mirror of the SoC sensor (%) |

There is also a **Dry run mode** switch on the same device (see above).

---

## Services

### `octopus_battery.set_mode`
Force the battery into a specific mode. Automatic control is suspended until the
override is cleared.

```yaml
service: octopus_battery.set_mode
data:
  mode: discharging   # idle | charging | discharging | top_up
  # entry_id: <config-entry-id>   # optional, to target one of several batteries
```

### `octopus_battery.clear_override`
Resume automatic control.

```yaml
service: octopus_battery.clear_override
data:
  # entry_id: <config-entry-id>   # optional
```

---

## Example: a typical day

With `use_hours = 4`, `charge_hours = 4` and a typical Agile price curve:

- **01:30 – 05:30** (cheapest 4 h) → `charging` (battery plug ON, load on mains)
- **05:30 – 16:00** → `idle` (battery full, load on mains)
- **16:00 – 20:00** (most expensive 4 h) → `discharging` (battery plug OFF, load on battery)
- **20:00 – 01:30** → `idle` / `top_up` if the battery has sagged below 3 %

---

## Development & testing

The core decision logic (`schedule.py`) is pure Python with **no Home Assistant
dependency**, so it can be unit-tested standalone:

```bash
python3 -m unittest discover -s tests -v
```

This runs the block-selection, mode-decision, and full-day simulation tests.

---

## Project layout

```
custom_components/octopus_battery/
├── __init__.py        # entry setup, services
├── config_flow.py     # UI configuration + options flow
├── const.py           # constants, defaults, modes
├── coordinator.py     # Octopus API price fetching (paginated)
├── controller.py      # state machine driving the two switches
├── helpers.py         # small shared helpers
├── schedule.py        # pure logic: block selection + mode decision
├── sensor.py          # status / schedule sensors
├── manifest.json
├── strings.json
└── translations/en.json
```
