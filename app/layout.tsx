import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: 'Queue Context — Matchmaking research',
  description: 'A local workspace for comparing pre-match player histories in League of Legends.',
  icons: {icon:'/favicon.svg'},
  openGraph: {title:'Queue Context',description:'Your matches, in context.'},
  twitter: {card:'summary',title:'Queue Context',description:'Your matches, in context.'},
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
