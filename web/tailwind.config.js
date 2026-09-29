/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // WCAG AA-safe on white/dark surfaces
        ink: '#0f172a',
        critical: '#b91c1c',
        warn: '#92400e',
        ok: '#065f46',
      },
    },
  },
  plugins: [],
};
