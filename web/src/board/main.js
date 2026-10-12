import { mount } from 'svelte';
import '../lib/base.css';
import Board from './Board.svelte';

mount(Board, { target: document.getElementById('app') });
