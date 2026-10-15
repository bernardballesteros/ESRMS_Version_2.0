// Tailwind theme for the organization dispatch dashboard.
tailwind.config = {
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', 'Inter', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        brand: {
          cyan: '#00e5ff',
          surface: '#0b0f19',
          card: '#0f1422',
          cardSubtle: '#141a2b',
          border: 'rgba(0, 229, 255, 0.18)',
          borderSubtle: 'rgba(255, 255, 255, 0.08)',
        },
      },
      boxShadow: {
        tacticalCyan: '0 0 20px -3px rgba(0, 229, 255, 0.25)',
      },
    },
  },
};
