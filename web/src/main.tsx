import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ThemeProvider } from 'next-themes'
import './index.css'
import App from './App'

// Inside the widget's expand iframe, force light to match the panel. forcedTheme
// renders the theme WITHOUT writing localStorage, so it never pollutes the
// standalone tab's saved preference (same origin) or its toggle.
const isEmbedded = typeof window !== 'undefined' && window.self !== window.top

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      forcedTheme={isEmbedded ? 'light' : undefined}
    >
      <App />
    </ThemeProvider>
  </StrictMode>
)
