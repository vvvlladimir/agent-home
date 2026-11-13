import { createApp } from 'vue';
import App from './App.vue';
import { start } from './store';
import './styles.css';

createApp(App).mount('#app');
start();
