import type {Metadata} from 'next';
import './globals.css';
export const metadata:Metadata={title:'Harvest · Know your market',description:'Explore mandi prices and daily agricultural price forecasts, with transparent model evaluation.'};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="en"><body>{children}</body></html>}
