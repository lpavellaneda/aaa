// Pegar en F12 > Consola en el VeloCloud real, antes de hacer el recorrido.
// No ejecuta consultas nuevas. Registra muestras de respuestas JSON.
(() => {
  if (window.VCOcaptura) {
    console.info('La captura ya está instalada. Usa VCOcaptura.descargar().');
    return;
  }
  const registros = [];
  const originalFetch = window.fetch;
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  const estados = new WeakMap();
  const secreto = /password|passwd|authorization|cookie|token|csrf|secret|credential|session/i;
  function muestra(valor, nivel = 0) {
    if (nivel > 10) return '[profundidad limitada]';
    if (Array.isArray(valor)) return {
      cantidad: valor.length,
      primeros: valor.slice(0, 3).map(v => muestra(v, nivel + 1))
    };
    if (valor && typeof valor === 'object') return Object.fromEntries(
      Object.entries(valor).map(([k, v]) => [k, secreto.test(k) ? '[omitido]' : muestra(v, nivel + 1)])
    );
    return typeof valor === 'string' ? valor.slice(0, 1000) : valor;
  }
  function limpiarURL(raw) {
    const u = new URL(raw, location.href);
    if (u.origin !== location.origin) return null;
    for (const k of [...u.searchParams.keys()]) {
      if (secreto.test(k)) u.searchParams.set(k, '[omitido]');
    }
    return u.pathname + u.search;
  }
  function cuerpo(raw) {
    if (raw == null) return null;
    if (typeof raw === 'string') {
      try { return muestra(JSON.parse(raw)); }
      catch { return '[cuerpo no JSON omitido]'; }
    }
    return '[cuerpo no textual omitido]';
  }
  function guardar(url, metodo, solicitud, estado, respuesta) {
    try {
      const ruta = limpiarURL(url);
      if (!ruta || registros.length >= 250) return;
      if (!respuesta || typeof respuesta !== 'object') return;
      registros.push({ ruta, metodo, solicitud, estado, respuesta: muestra(respuesta) });
      console.info(`[VCO captura ${registros.length}] ${metodo} ${ruta}`);
    } catch { /* No interrumpir la aplicación. */ }
  }
  window.fetch = function(input, init) {
    const url = input instanceof Request ? input.url : String(input);
    const metodo = init?.method || (input instanceof Request ? input.method : 'GET');
    const solicitud = init?.body !== undefined
      ? Promise.resolve(cuerpo(init.body))
      : input instanceof Request
        ? input.clone().text().then(cuerpo).catch(() => '[no disponible]')
        : Promise.resolve(null);
    const pendiente = originalFetch.apply(this, arguments);
    pendiente.then(res => {
      if (!limpiarURL(url)) return;
      const copia = res.clone();
      Promise.all([solicitud, copia.json()])
        .then(([req, data]) => guardar(url, metodo, req, res.status, data))
        .catch(() => {});
    }).catch(() => {});
    return pendiente;
  };
  XMLHttpRequest.prototype.open = function(metodo, url) {
    const resultado = originalOpen.apply(this, arguments);
    estados.set(this, { metodo, url: String(url) });
    return resultado;
  };
  XMLHttpRequest.prototype.send = function(body) {
    const datos = estados.get(this);
    if (datos) {
      const solicitud = cuerpo(body);
      this.addEventListener('load', () => {
        try {
          const respuesta = this.responseType === 'json' ? this.response
            : (!this.responseType || this.responseType === 'text')
              ? JSON.parse(this.responseText) : null;
          guardar(datos.url, datos.metodo, solicitud, this.status, respuesta);
        } catch { /* Ignorar respuestas no JSON. */ }
      }, { once: true });
    }
    return originalSend.apply(this, arguments);
  };
  window.VCOcaptura = {
    registros,
    descargar() {
      const blob = new Blob([JSON.stringify({
        zonaHorariaNavegador: Intl.DateTimeFormat().resolvedOptions().timeZone,
        registros
      }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'velocloud-consultas.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
      console.info(`Descargados ${registros.length} registros. Contienen muestras de datos de tu red.`);
    },
    detener() {
      window.fetch = originalFetch;
      XMLHttpRequest.prototype.open = originalOpen;
      XMLHttpRequest.prototype.send = originalSend;
      delete window.VCOcaptura;
      console.info('Captura detenida.');
    }
  };
  console.info('Captura activa. Sin recargar la pestaña, actualiza la lista, abre una agencia, selecciona el mes y entra a Vínculos > Rendimiento promedio. Luego ejecuta VCOcaptura.descargar().');
})();
