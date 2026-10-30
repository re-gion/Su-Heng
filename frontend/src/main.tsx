import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { AuthGate } from './AuthGate'

createRoot(document.getElementById('root')!).render(
  <StrictMode><AuthGate>{(auth) => <App auth={auth} />}</AuthGate></StrictMode>,
)
