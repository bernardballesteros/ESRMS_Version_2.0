// Tailwind theme for the portal, login and dashboard screens.
tailwind.config = {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        bgDark: '#050A12',
        cardBg: '#0D1420',
        cardSubtle: '#101A27',
        cyanAccent: '#00D9FF',
        blueSecondary: '#1685FF',
        textLight: '#F1F5F9',
        textMuted: '#8B98A8',
        borderGlow: 'rgba(0, 217, 255, 0.22)',
        tactical: {
          950: '#06090f',
          900: '#0a0e17',
          850: '#0f1523',
          800: '#141d2f',
          700: '#1e2b45',
        },
      },
    },
  },
};
