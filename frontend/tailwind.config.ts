import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Semantic tokens; refined when the design system lands in later phases.
        surface: {
          DEFAULT: '#0f1115',
          raised: '#161a21',
          overlay: '#1e242e',
        },
        edge: {
          positive: '#22c55e',
          negative: '#ef4444',
        },
      },
    },
  },
  plugins: [],
} satisfies Config
