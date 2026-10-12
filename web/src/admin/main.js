import { mount } from 'svelte';
import '../lib/base.css';
import Admin from './Admin.svelte';

mount(Admin, { target: document.getElementById('app') });
