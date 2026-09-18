import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';

export default mount(App, { target: document.getElementById('app') });

// Installable on a phone: the service worker caches the shell and the hashed assets, never the
// API, so "Add to Home Screen" gives an app that opens straight to what is on.
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
