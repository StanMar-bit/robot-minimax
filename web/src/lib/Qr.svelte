<script>
  import QRCode from 'qrcode';

  let { text, size = 200 } = $props();
  let svg = $state('');

  $effect(() => {
    const t = text;
    if (!t) return;
    QRCode.toString(t, { type: 'svg', margin: 1, errorCorrectionLevel: 'M', color: { dark: '#111827', light: '#ffffff' } })
      .then((s) => (svg = s))
      .catch(() => (svg = ''));
  });
</script>

<div class="qr" style:width="{size}px" style:height="{size}px" role="img" aria-label="QR code for {text}">
  <!-- eslint-disable-next-line svelte/no-at-html-tags -- generated locally by the qrcode library -->
  {@html svg}
</div>

<style>
  .qr {
    background: #fff;
    flex: none;
  }
  .qr :global(svg) {
    width: 100%;
    height: 100%;
    display: block;
  }
</style>
