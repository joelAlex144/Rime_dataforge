import { Route, Routes } from 'react-router-dom'
import Listener from './routes/Listener'
import Dev from './routes/Dev'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Listener />} />
      <Route path="/dev" element={<Dev />} />
    </Routes>
  )
}
