/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontSize: {
        xs:   ['14px', { lineHeight: '1.2rem' }],
        sm:   ['16px', { lineHeight: '1.45rem' }],
        base: ['18px', { lineHeight: '1.65rem' }],
        lg:   ['21px', { lineHeight: '1.85rem' }],
        xl:   ['24px', { lineHeight: '2rem' }],
      },
      colors: {
        navy: {
          950: '#060b18',
          900: '#0a1628',
          800: '#0f2044',
          700: '#162d5c',
          600: '#1e3a73',
        },
        slate: {
          850: '#1a2535',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
