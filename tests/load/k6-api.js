// k6 load profile for the FleetSentinel API (runs on a Codespace or the demo VM, not the laptop).
// Usage: k6 run -e BASE_URL=https://app.<ip>.sslip.io tests/load/k6-api.js
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RPS || 200), timeUnit: '1s',
      duration: '5m', preAllocatedVUs: 50, maxVUs: 200,
    },
    burst: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.BURST_RPS || 600), timeUnit: '1s',
      duration: '5m', preAllocatedVUs: 100, maxVUs: 400,
      startTime: '5m',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const TOKEN = __ENV.API_TOKEN || 'dev';

export default function () {
  const h = { headers: { Authorization: `Bearer ${TOKEN}` } };
  const r1 = http.get(`${BASE}/v1/fleet/summary`, h);
  check(r1, { 'summary 200': (r) => r.status === 200 });
  const r2 = http.get(`${BASE}/v1/alerts?limit=50`, h);
  check(r2, { 'alerts 200': (r) => r.status === 200 });
  sleep(0.5);
}

export function handleSummary(data) {
  return { 'k6-summary.json': JSON.stringify(data) };
}
