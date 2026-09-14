import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: 'Queue Lab — Matchmaking research',
  description: 'A local workspace for comparing pre-match player histories in League of Legends.',
  icons: {icon:'/favicon.svg'},
  openGraph: {title:'Queue Lab',description:'Your matches, in context.'},
  twitter: {card:'summary',title:'Queue Lab',description:'Your matches, in context.'},
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
