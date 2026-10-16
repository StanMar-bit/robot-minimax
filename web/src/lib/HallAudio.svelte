<script>
  // Plays the robot's speech through the hall speakers. The bridge renders each line on the
  // robot in its own voice and the server sends it here as the robot starts speaking.
  // Browsers only play sound after a click on the page, hence the button.
  let { socket } = $props();

  let enabled = $state(false);
  let speaking = $state(false);
  let player = null;

  function stop() {
    if (!player) return;
    player.pause();
    URL.revokeObjectURL(player.src);
    player = null;
    speaking = false;
  }

  $effect(() => {
    const onSpeak = ({ audio, type }) => {
      if (!enabled || !audio) return;
      stop();
      const a = new Audio(URL.createObjectURL(new Blob([audio], { type })));
      a.onended = stop;
      player = a;
      speaking = true;
      a.play().catch(() => {
        // the browser refused (no click yet): ask for one
        enabled = false;
        stop();
      });
    };
    socket.on('speak', onSpeak);
    return () => socket.off('speak', onSpeak);
  });

  function toggle() {
    enabled = !enabled;
    if (!enabled) stop();
  }
</script>

<button class="hall" class:on={enabled} class:speaking onclick={toggle} title="Robot voice through the hall speakers">
  {enabled ? (speaking ? '🔊 Robot speaking' : '🔊 Robot voice on') : '🔇 Click to enable robot voice'}
</button>

<style>
  .hall {
    position: fixed;
    left: 12px;
    bottom: 12px;
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid var(--line);
    background: var(--warn);
    color: #fff;
    font: 600 14px var(--font);
    cursor: pointer;
    z-index: 10;
  }
  .hall.on {
    background: var(--panel);
    color: var(--muted);
    opacity: 0.6;
  }
  .hall.on.speaking {
    color: var(--ink);
    opacity: 1;
  }
</style>
