import { mount } from 'svelte';
import '../lib/base.css';
import Phone from './Phone.svelte';

mount(Phone, { target: document.getElementById('app') });
