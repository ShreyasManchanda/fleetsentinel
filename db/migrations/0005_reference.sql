-- 0005_reference: global reference data. Synthetic OEMs/models (no real brands), tariffs in USD-equivalent, DTC lookup.
INSERT INTO oem (code, name) VALUES
  ('OEM_A', 'Aurora Motors'),
  ('OEM_B', 'Kestrel Automotive'),
  ('OEM_C', 'Meridian Mobility'),
  ('OEM_D', 'Nimbus Electric')
ON CONFLICT (code) DO NOTHING;

INSERT INTO vehicle_model (oem_id, name, powertrain, battery_kwh, idle_fuel_l_per_h, idle_kw)
SELECT o.id, v.name, v.powertrain, v.battery_kwh, v.idle_fuel, v.idle_kw
FROM (VALUES
  ('OEM_A', 'Aurora Sprint',  'ICE',    NULL::numeric, 0.900::numeric, NULL::numeric),
  ('OEM_A', 'Aurora Volt',    'EV',     60.0,          NULL,           1.200),
  ('OEM_B', 'Kestrel Hauler', 'ICE',    NULL,          1.600,          NULL),
  ('OEM_B', 'Kestrel Hybrid', 'HYBRID', 14.0,          0.600,          0.400),
  ('OEM_C', 'Meridian City',  'ICE',    NULL,          0.700,          NULL),
  ('OEM_C', 'Meridian E',     'EV',     75.0,          NULL,           1.400),
  ('OEM_D', 'Nimbus One',     'EV',     82.0,          NULL,           1.500),
  ('OEM_D', 'Nimbus Lite',    'EV',     48.0,          NULL,           1.000)
) AS v(oem_code, name, powertrain, battery_kwh, idle_fuel, idle_kw)
JOIN oem o ON o.code = v.oem_code
ON CONFLICT (oem_id, name) DO NOTHING;

INSERT INTO tariff_region (code, name, currency) VALUES
  ('IN-KA', 'Karnataka, India (USD-equivalent)', 'USD'),
  ('US-CA', 'California, USA', 'USD')
ON CONFLICT (code) DO NOTHING;

INSERT INTO tariff_rate (tariff_region_id, energy_type, hour_from, hour_to, price_per_unit, unit)
SELECT r.id, t.energy_type, t.hour_from, t.hour_to, t.price, t.unit
FROM (VALUES
  ('IN-KA', 'DIESEL',      0, 24, 1.10,  'L'),
  ('IN-KA', 'PETROL',      0, 24, 1.25,  'L'),
  ('IN-KA', 'ELECTRICITY', 0,  6, 0.070, 'kWh'),
  ('IN-KA', 'ELECTRICITY', 6, 18, 0.120, 'kWh'),
  ('IN-KA', 'ELECTRICITY', 18, 22, 0.180, 'kWh'),
  ('IN-KA', 'ELECTRICITY', 22, 24, 0.090, 'kWh'),
  ('US-CA', 'DIESEL',      0, 24, 1.60,  'L'),
  ('US-CA', 'PETROL',      0, 24, 1.30,  'L'),
  ('US-CA', 'ELECTRICITY', 0, 16, 0.220, 'kWh'),
  ('US-CA', 'ELECTRICITY', 16, 21, 0.480, 'kWh'),
  ('US-CA', 'ELECTRICITY', 21, 24, 0.260, 'kWh')
) AS t(region, energy_type, hour_from, hour_to, price, unit)
JOIN tariff_region r ON r.code = t.region
ON CONFLICT (tariff_region_id, energy_type, hour_from) DO NOTHING;

INSERT INTO dtc_code (code, system, generic, description, critical) VALUES
  ('B0001', 'body', true, 'Driver frontal stage-1 deployment control', true),
  ('C0035', 'chassis', true, 'Left front wheel speed sensor circuit', false),
  ('C0040', 'chassis', true, 'Right front wheel speed sensor circuit', false),
  ('P0171', 'powertrain', true, 'System too lean (bank 1)', false),
  ('P0172', 'powertrain', true, 'System too rich (bank 1)', false),
  ('P0217', 'powertrain', true, 'Engine coolant over-temperature condition', true),
  ('P0300', 'powertrain', true, 'Random/multiple cylinder misfire detected', true),
  ('P0301', 'powertrain', true, 'Cylinder 1 misfire detected', false),
  ('P0302', 'powertrain', true, 'Cylinder 2 misfire detected', false),
  ('P0303', 'powertrain', true, 'Cylinder 3 misfire detected', false),
  ('P0304', 'powertrain', true, 'Cylinder 4 misfire detected', false),
  ('P0420', 'powertrain', true, 'Catalyst system efficiency below threshold (bank 1)', false),
  ('P0442', 'powertrain', true, 'Evaporative emission system leak detected (small)', false),
  ('P0455', 'powertrain', true, 'Evaporative emission system leak detected (large)', false),
  ('P0524', 'powertrain', true, 'Engine oil pressure too low', true),
  ('P0562', 'powertrain', true, 'System voltage low', false),
  ('P0601', 'powertrain', true, 'Internal control module memory checksum error', false),
  ('P0700', 'powertrain', true, 'Transmission control system malfunction', false),
  ('P0A0D', 'powertrain', true, 'Drive motor / high-voltage interlock circuit high', true),
  ('P0A80', 'powertrain', true, 'Replace hybrid/EV battery pack', false),
  ('P0AA6', 'powertrain', true, 'High-voltage isolation fault', true),
  ('P0AFA', 'powertrain', true, 'Hybrid/EV battery system voltage low', true),
  ('P1A00', 'powertrain', false, 'Battery thermal management performance', false),
  ('U0100', 'network', true, 'Lost communication with ECM/PCM', true),
  ('U0121', 'network', true, 'Lost communication with ABS control module', false)
ON CONFLICT (code) DO NOTHING;
