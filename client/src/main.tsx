import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { StartupIntro } from './components/StartupIntro'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><StartupIntro><App /></StartupIntro></React.StrictMode>)
