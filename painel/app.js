/* ===================================================================
   Painel do Ditado - interface.
   Regras deste arquivo:
     - NADA de innerHTML com dado vindo do banco. Todo texto de ditado
       entra por textContent ou por no de texto (document.createTextNode).
     - O realce da busca e' construido no CLIENTE, com <mark> criado por
       createElement. O campo "destaque" da API (snippet do FTS5) NAO e'
       consumido: ele e' HTML, e HTML vindo do texto ditado so entraria
       na pagina por innerHTML.
     - Nenhum recurso externo. Nenhum eval. Nenhum timer que sobreviva
       a aba escondida.
   =================================================================== */

(function () {
  'use strict';

  /* -------- heranca de service worker desta porta (limpeza defensiva) -- */
  if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
    navigator.serviceWorker.getRegistrations().then(function (regs) {
      regs.forEach(function (r) { r.unregister(); });
    }).catch(function () {});
  }

  var $ = function (id) { return document.getElementById(id); };

  /* =================================================================
     1. UTILITARIOS
     ================================================================= */

  var DIAS_SEMANA = ['domingo', 'segunda-feira', 'terca-feira', 'quarta-feira',
                     'quinta-feira', 'sexta-feira', 'sabado'];
  var MESES = ['janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho',
               'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];

  function zero(n) { return (n < 10 ? '0' : '') + n; }

  /* 1.234 - separador de milhar em pt-BR, sem depender de Intl */
  function num(n) {
    var s = String(Math.round(n || 0)), saida = '', c = 0, i;
    for (i = s.length - 1; i >= 0; i--) {
      saida = s.charAt(i) + saida;
      c++;
      if (c % 3 === 0 && i > 0) { saida = '.' + saida; }
    }
    return saida;
  }

  /* duracao sempre em M:SS - mesmo formato do timer da pilula */
  function mss(seg) {
    var s = Math.max(0, Math.round(seg || 0));
    return Math.floor(s / 60) + ':' + zero(s % 60);
  }

  function umaCasa(v) { return (Math.round((v || 0) * 10) / 10).toString().replace('.', ','); }

  /* o timestamp do banco e' float epoch: sem o *1000 tudo cai em 1970 */
  function dataDe(ts) { return new Date((ts || 0) * 1000); }
  function horaDe(ts) { var d = dataDe(ts); return zero(d.getHours()) + ':' + zero(d.getMinutes()); }

  /* "2026-08-05" -> Date local. new Date("2026-08-05") seria UTC e pularia o dia */
  function diaParaData(dia) {
    var p = String(dia || '').split('-');
    if (p.length !== 3) { return null; }
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function dataParaDia(d) {
    return d.getFullYear() + '-' + zero(d.getMonth() + 1) + '-' + zero(d.getDate());
  }

  function hojeDia() { return dataParaDia(new Date()); }

  function ontemDia() {
    var d = new Date(); d.setDate(d.getDate() - 1); return dataParaDia(d);
  }

  function ddmm(dia) {
    var p = String(dia || '').split('-');
    return p.length === 3 ? (p[2] + '/' + p[1]) : String(dia || '');
  }

  /* cabecalho de dia: "HOJE - quinta, 5 de agosto" (a caixa alta e' do CSS) */
  function rotuloDia(dia) {
    var d = diaParaData(dia);
    if (!d) { return String(dia || ''); }
    var corpo = DIAS_SEMANA[d.getDay()] + ', ' + d.getDate() + ' de ' + MESES[d.getMonth()];
    if (dia === hojeDia()) { return 'hoje - ' + corpo; }
    if (dia === ontemDia()) { return 'ontem - ' + corpo; }
    return corpo;
  }

  /* dobra acentos e caixa SEM mudar o comprimento (indices continuam valendo) */
  var ACENTOS = {
    'á': 'a', 'à': 'a', 'ã': 'a', 'â': 'a', 'ä': 'a', 'é': 'e', 'ê': 'e', 'è': 'e',
    'ë': 'e', 'í': 'i', 'ì': 'i', 'î': 'i', 'ï': 'i', 'ó': 'o', 'ô': 'o', 'õ': 'o',
    'ò': 'o', 'ö': 'o', 'ú': 'u', 'ù': 'u', 'û': 'u', 'ü': 'u', 'ç': 'c', 'ñ': 'n'
  };
  function dobrar(s) {
    var t = String(s || '').toLowerCase(), r = '', i, c;
    for (i = 0; i < t.length; i++) { c = t.charAt(i); r += (ACENTOS[c] || c); }
    return r;
  }

  function reduzido() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function anunciar(texto) { $('viva').textContent = texto; }

  function limpar(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  function criar(tag, classe, texto) {
    var el = document.createElement(tag);
    if (classe) { el.className = classe; }
    if (texto !== undefined && texto !== null) { el.textContent = texto; }
    return el;
  }

  function guardar(chave, valor) {
    try { window.localStorage.setItem('painel-wispr.' + chave, valor); } catch (e) {}
  }
  function lembrar(chave, padrao) {
    try {
      var v = window.localStorage.getItem('painel-wispr.' + chave);
      return v === null ? padrao : v;
    } catch (e) { return padrao; }
  }

  /* =================================================================
     2. REDE
     ================================================================= */

  var bloqueado = false;

  function mostrarBloqueio() {
    if (!bloqueado) { bloqueado = true; }
    var caixa = $('bloqueio');
    caixa.classList.remove('oculta');
    /* O dialogo e terminal: enquanto a sessao esta expirada, nenhum controle
       do historico deve continuar tabulavel ou acionavel por teclado. */
    ['.pular', '#topo', '#conteudo'].forEach(function (seletor) {
      var el = document.querySelector(seletor);
      if (!el) { return; }
      el.inert = true;
      el.setAttribute('aria-hidden', 'true');
    });
    try { caixa.focus(); } catch (e) {}
  }

  /* -------- sessao: o segredo mora no sessionStorage DESTA PORTA --------
     Por que nao cookie: cookie e cego a porta (a RFC 6265 nao isola por
     porta), entao QUALQUER outro servidor local que este Chrome visitasse
     receberia o segredo do painel nos headers e poderia ler o historico
     medico inteiro. Ja o sessionStorage e escopado por origem completa
     (esquema + host + PORTA) e nenhuma outra pagina alcanca.
     O token chega UMA vez, dentro do HTML, na request que trouxe o ticket;
     daqui pra frente ele viaja no cabecalho X-Painel-Sessao.             */

  var CHAVE_SESSAO = 'painel-wispr.sessao';
  var sessao = '';

  function iniciarSessao() {
    var meta = document.querySelector('meta[name="painel-sessao"]');
    var novo = meta ? (meta.getAttribute('content') || '') : '';
    /* tira da marcacao logo: nao precisa ficar legivel no DOM */
    if (meta && meta.parentNode) { meta.parentNode.removeChild(meta); }
    if (novo) {
      try { window.sessionStorage.setItem(CHAVE_SESSAO, novo); } catch (e) {}
      sessao = novo;
      /* apaga o ?t= da barra: o ticket ja foi queimado, nao serve mais */
      if (window.history && window.history.replaceState && window.location.search) {
        try { window.history.replaceState(null, '', '/'); } catch (e) {}
      }
      return;
    }
    try { sessao = window.sessionStorage.getItem(CHAVE_SESSAO) || ''; }
    catch (e) { sessao = ''; }
  }

  function cabecalhosSessao(extras) {
    var h = extras || {};
    h['X-Painel-Sessao'] = sessao;
    return h;
  }

  function tratarStatus(r) {
    if (r.status === 403) {
      mostrarBloqueio();
      throw new Error('sessao');
    }
    if (!r.ok) { throw new Error('http ' + r.status); }
    return r.json();
  }

  function pedir(rota) {
    return fetch(rota, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: cabecalhosSessao()
    }).then(tratarStatus);
  }

  function postar(rota, corpo) {
    return fetch(rota, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: cabecalhosSessao({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(corpo || {})
    }).then(tratarStatus);
  }

  /* =================================================================
     3. ESTADO
     ================================================================= */

  var estado = {
    aba: lembrar('aba', 'historico'),
    q: '',
    modoDia: lembrar('modoDia', ''),     /* '' | hoje | ontem | outro */
    data: lembrar('data', ''),           /* YYYY-MM-DD quando modoDia = outro */
    app: lembrar('app', ''),
    pagina: 0,
    porPagina: 60,
    total: 0,
    totalGeral: 0,          /* total do banco, vindo do /api/resumo */
    carregando: false,
    fim: false,
    geracao: 0,                          /* descarta resposta de consulta velha */
    aberto: null,                        /* id do ditado expandido */
    saude: null,
    timerSaude: null
  };

  function diaEscolhido() {
    if (estado.modoDia === 'hoje') { return hojeDia(); }
    if (estado.modoDia === 'ontem') { return ontemDia(); }
    if (estado.modoDia === 'outro') { return estado.data || ''; }
    return '';
  }

  function temFiltro() { return !!(diaEscolhido() || estado.app); }

  function nomeDoApp(bundle) {
    var sel = $('filtro-app'), i;
    for (i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === bundle) { return sel.options[i].dataset.nome || bundle; }
    }
    return bundle;
  }

  /* =================================================================
     4. REALCE DA BUSCA (100% no cliente, sem innerHTML)
     ================================================================= */

  function termosDe(q) {
    var cru = String(q || '').replace(/["()*]/g, ' ').split(/\s+/), saida = [], i, t;
    for (i = 0; i < cru.length; i++) {
      t = dobrar(cru[i]).replace(/[^0-9a-zÀ-ɏ]/g, '');
      if (t.length >= 2 && t !== 'or' && t !== 'and' && t !== 'not') { saida.push(t); }
    }
    return saida;
  }

  function faixasDe(texto, alvos) {
    var f = dobrar(texto), achados = [], i, k, de, j;
    for (k = 0; k < alvos.length; k++) {
      de = 0;
      while (achados.length < 500) {
        j = f.indexOf(alvos[k], de);
        if (j === -1) { break; }
        achados.push([j, j + alvos[k].length]);
        de = j + alvos[k].length;
      }
    }
    achados.sort(function (a, b) { return a[0] - b[0]; });
    var juntas = [];
    for (i = 0; i < achados.length; i++) {
      if (juntas.length && achados[i][0] <= juntas[juntas.length - 1][1]) {
        juntas[juntas.length - 1][1] = Math.max(juntas[juntas.length - 1][1], achados[i][1]);
      } else { juntas.push([achados[i][0], achados[i][1]]); }
    }
    return juntas;
  }

  /* escreve o texto no elemento, com <mark> nos trechos casados.

     AVISO A QUEM FOR "OTIMIZAR" ISTO: a API devolve um campo `destaque`
     com o snippet do FTS5 ja marcado em HTML. NAO USE esse campo, e nunca
     atribua ele (nem nada vindo do banco) a innerHTML. O realce aqui e'
     reconstruido no cliente exatamente para que texto de ditado jamais
     entre na pagina como marcacao. O `destaque` existe so porque o
     contrato da API o exige.                                            */
  function preencher(el, texto, q) {
    limpar(el);
    var alvos = termosDe(q);
    if (!alvos.length) { el.textContent = texto; return; }
    var faixas = faixasDe(texto, alvos);
    if (!faixas.length) { el.textContent = texto; return; }
    var pos = 0, i, m;
    for (i = 0; i < faixas.length; i++) {
      if (faixas[i][0] > pos) {
        el.appendChild(document.createTextNode(texto.slice(pos, faixas[i][0])));
      }
      m = criar('mark', null, texto.slice(faixas[i][0], faixas[i][1]));
      el.appendChild(m);
      pos = faixas[i][1];
    }
    if (pos < texto.length) { el.appendChild(document.createTextNode(texto.slice(pos))); }
  }

  /* janela de ~300 caracteres em volta da primeira ocorrencia */
  function trecho(texto, q) {
    var alvos = termosDe(q);
    if (!alvos.length) { return null; }
    var f = dobrar(texto), i = -1, k, j;
    for (k = 0; k < alvos.length; k++) {
      j = f.indexOf(alvos[k]);
      if (j !== -1 && (i === -1 || j < i)) { i = j; }
    }
    if (i === -1) { return null; }
    var ini = Math.max(0, i - 90), esp;
    if (ini > 0) {
      esp = texto.indexOf(' ', ini);
      if (esp !== -1 && esp < i) { ini = esp + 1; }
    }
    var fim = Math.min(texto.length, ini + 300);
    return (ini > 0 ? '...' : '') + texto.slice(ini, fim) + (fim < texto.length ? '...' : '');
  }

  /* =================================================================
     5. COPIAR - a acao central do painel
     ================================================================= */

  function selecionarNo(el) {
    try {
      var faixa = document.createRange();
      faixa.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(faixa);
    } catch (e) {}
  }

  function copiaAntiga(texto) {
    var area = document.createElement('textarea');
    area.value = texto;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, area.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }

  function copiar(botao, texto, cartao) {
    var volta = function () {
      clearTimeout(botao._t);
      botao._t = setTimeout(function () {
        botao.classList.remove('copiado');
        botao.classList.remove('falhou');
        botao.textContent = 'Copiar';
      }, 1600);
    };
    var deuCerto = function () {
      botao.classList.remove('falhou');
      botao.classList.add('copiado');
      botao.textContent = 'Copiado';
      anunciar('Texto copiado');
      volta();
    };
    var deuErrado = function () {
      botao.classList.remove('copiado');
      botao.classList.add('falhou');
      botao.textContent = 'Nao copiou';
      /* nunca mentir que copiou: abre o ditado e seleciona pra ele dar Ctrl+C */
      abrirCartao(cartao, true);
      var alvo = cartao.querySelector('.texto');
      if (alvo) { selecionarNo(alvo); }
      anunciar('Nao consegui copiar. O texto ficou selecionado, use Controle C.');
      clearTimeout(botao._t);
      botao._t = setTimeout(function () {
        botao.classList.remove('falhou');
        botao.textContent = 'Copiar';
      }, 3200);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(texto).then(deuCerto, function () {
        if (copiaAntiga(texto)) { deuCerto(); } else { deuErrado(); }
      });
    } else if (copiaAntiga(texto)) {
      deuCerto();
    } else {
      deuErrado();
    }
  }

  /* =================================================================
     6. HISTORICO - cartoes
     ================================================================= */

  var lista = $('lista');
  var contexto = { dia: null, secao: null, contador: null, n: 0 };

  function animarAltura(el, de, para, aoFim) {
    if (reduzido()) { if (aoFim) { aoFim(); } return; }
    el.classList.add('animando');
    el.style.height = de + 'px';
    void el.offsetHeight;
    el.style.height = para + 'px';
    clearTimeout(el._anim);
    el._anim = setTimeout(function () {
      el.classList.remove('animando');
      el.style.height = '';
      if (aoFim) { aoFim(); }
    }, 200);
  }

  function fecharCartao(cartao) {
    if (!cartao) { return; }
    var corpo = cartao.querySelector('.corpo');
    var topo = cartao.querySelector('.ditado-topo');
    var previa = cartao.querySelector('.previa-caixa');
    topo.setAttribute('aria-expanded', 'false');
    cartao.classList.remove('aberto');
    if (previa) { previa.classList.remove('oculta'); }
    animarAltura(corpo, corpo.scrollHeight, 0, function () { corpo.hidden = true; });
    if (estado.aberto === cartao.dataset.id) { estado.aberto = null; }
  }

  function abrirCartao(cartao, semAnimacao) {
    if (!cartao || cartao.classList.contains('aberto')) { return; }
    var anterior = lista.querySelector('.ditado.aberto');
    if (anterior && anterior !== cartao) { fecharCartao(anterior); }

    var corpo = cartao.querySelector('.corpo');
    var topo = cartao.querySelector('.ditado-topo');
    var previa = cartao.querySelector('.previa-caixa');
    var item = cartao._item;

    /* o texto integral so entra no DOM quando ele abre */
    if (!cartao._montado) {
      preencher(cartao.querySelector('.texto'), item.texto || '', estado.q);
      montarMeta(cartao.querySelector('.meta'), item);
      cartao._montado = true;
    }

    corpo.hidden = false;
    topo.setAttribute('aria-expanded', 'true');
    cartao.classList.add('aberto');
    if (previa) { previa.classList.add('oculta'); }
    estado.aberto = cartao.dataset.id;
    if (!semAnimacao) { animarAltura(corpo, 0, corpo.scrollHeight); }
  }

  function montarMeta(el, item) {
    limpar(el);
    var pedacos = [];
    pedacos.push('falou ' + mss(item.duracao_s));
    if (item.transcricao_s > 0) { pedacos.push('transcreveu em ' + umaCasa(item.transcricao_s) + ' s'); }
    if (item.palavras) { pedacos.push(num(item.palavras) + ' palavras'); }
    pedacos.push(num((item.texto || '').length) + ' caracteres');
    if (item.perfil && item.perfil !== 'livre') { pedacos.push('perfil ' + item.perfil); }

    pedacos.forEach(function (p, i) {
      if (i) { el.appendChild(criar('span', 'sep', '·')); }
      el.appendChild(document.createTextNode(p));
    });

    if (item.polish && String(item.polish).indexOf('skip') !== 0) {
      el.appendChild(criar('span', 'sep', '·'));
      el.appendChild(criar('span', 'polish', 'polish ' + item.polish));
    }
    if (item.app_bundle) {
      el.appendChild(criar('span', 'sep', '·'));
      el.appendChild(criar('code', null, item.app_bundle));
    }
  }

  function montarCartao(item) {
    var art = criar('article', 'ditado');
    art.dataset.id = String(item.id);
    art._item = item;

    var h3 = criar('h3');
    var topo = criar('button', 'ditado-topo');
    topo.type = 'button';
    topo.setAttribute('aria-expanded', 'false');
    topo.id = 'topo-' + item.id;
    topo.setAttribute('aria-controls', 'd-' + item.id);

    topo.appendChild(criar('span', 'hora', horaDe(item.ts)));
    topo.appendChild(criar('span', 'dur', mss(item.duracao_s)));

    /* faixa de duracao: escala sqrt, senao os ditados curtos somem */
    var faixa = criar('span', 'faixa');
    faixa.setAttribute('aria-hidden', 'true');
    var barra = criar('i');
    barra.style.width = (Math.sqrt(Math.min(item.duracao_s || 0, 120) / 120) * 100).toFixed(1) + '%';
    faixa.appendChild(barra);
    topo.appendChild(faixa);

    topo.appendChild(criar('span', 'destino', item.app_nome || '(desconhecido)'));
    h3.appendChild(topo);
    art.appendChild(h3);

    /* o Copiar e' IRMAO do toggle: botao dentro de botao e' HTML invalido */
    var copia = criar('button', 'copiar', 'Copiar');
    copia.type = 'button';
    copia.setAttribute('aria-label', 'Copiar o ditado das ' + horaDe(item.ts));
    art.appendChild(copia);

    var caixaPrevia = criar('div', 'previa-caixa');
    var previa = criar('p', 'previa');
    var recorte = estado.q ? trecho(item.texto || '', estado.q) : null;
    if (recorte === null) {
      recorte = item.previa || '';
      /* a previa vem cortada em ~240 caracteres: avisa que continua */
      if ((item.texto || '').length > recorte.length) { recorte += '...'; }
    }
    preencher(previa, recorte, estado.q);
    caixaPrevia.appendChild(previa);
    art.appendChild(caixaPrevia);

    var corpo = criar('div', 'corpo');
    corpo.id = 'd-' + item.id;
    corpo.hidden = true;
    corpo.appendChild(criar('p', 'texto'));
    corpo.appendChild(criar('p', 'meta'));
    art.appendChild(corpo);

    topo.addEventListener('click', function () {
      if (art.classList.contains('aberto')) { fecharCartao(art); } else { abrirCartao(art); }
    });
    copia.addEventListener('click', function () { copiar(copia, item.texto || '', art); });

    return art;
  }

  function abrirDia(dia) {
    var secao = criar('section');
    var cab = criar('h2', 'dia');
    cab.appendChild(criar('span', 'dia-nome', rotuloDia(dia)));
    var contador = criar('span', 'dia-n', '');
    cab.appendChild(contador);
    secao.appendChild(cab);
    lista.appendChild(secao);
    contexto.dia = dia;
    contexto.secao = secao;
    contexto.contador = contador;
    contexto.n = 0;
  }

  function acrescentar(itens) {
    itens.forEach(function (item) {
      if (item.dia !== contexto.dia) { abrirDia(item.dia); }
      contexto.n += 1;
      contexto.contador.textContent = contexto.n === 1 ? '1 ditado' : num(contexto.n) + ' ditados';
      contexto.secao.appendChild(montarCartao(item));
    });
  }

  /* -------------------- estados vazios / esqueleto -------------------- */

  /* clona um icone ja embutido no HTML (sem createElementNS, sem arquivo externo) */
  function molde(id) {
    var no = $(id);
    if (!no) { return criar('span'); }
    var copia = no.cloneNode(true);
    copia.removeAttribute('id');
    copia.setAttribute('aria-hidden', 'true');
    return copia;
  }

  function iconeMic() { return molde('molde-mic'); }

  function mostrarEsqueleto() {
    limpar(lista);
    contexto.dia = null;
    var larguras = ['38%', '88%', '64%'], i, j, cartao, barra;
    for (i = 0; i < 4; i++) {
      cartao = criar('div', 'esq');
      for (j = 0; j < larguras.length; j++) {
        barra = criar('div', 'esq-barra');
        barra.style.width = larguras[j];
        cartao.appendChild(barra);
      }
      lista.appendChild(cartao);
    }
  }

  function mostrarVazio(titulo, texto, rotuloBotao, aoClicar, extra) {
    limpar(lista);
    contexto.dia = null;
    var caixa = criar('div', 'vazio');
    caixa.appendChild(iconeMic());
    caixa.appendChild(criar('h2', null, titulo));
    caixa.appendChild(criar('p', null, texto));
    if (extra) { caixa.appendChild(criar('code', null, extra)); }
    if (rotuloBotao) {
      var b = criar('button', 'botao', rotuloBotao);
      b.type = 'button';
      b.addEventListener('click', aoClicar);
      caixa.appendChild(b);
    }
    lista.appendChild(caixa);
  }

  function descricaoFiltro() {
    var partes = [];
    if (estado.app) { partes.push('em ' + nomeDoApp(estado.app)); }
    var d = diaEscolhido();
    if (d) {
      partes.push(d === hojeDia() ? 'hoje' : (d === ontemDia() ? 'ontem' : 'em ' + ddmm(d)));
    }
    return partes.join(' ');
  }

  /* -------------------- carga e paginacao -------------------- */

  function montarUrl() {
    var p = [];
    p.push('pagina=' + estado.pagina);
    p.push('por_pagina=' + estado.porPagina);
    if (estado.q) { p.push('q=' + encodeURIComponent(estado.q)); }
    var d = diaEscolhido();
    if (d) { p.push('dia=' + encodeURIComponent(d)); }
    if (estado.app) { p.push('app=' + encodeURIComponent(estado.app)); }
    return '/api/historico?' + p.join('&');
  }

  function atualizarContagem() {
    var el = $('contagem'), texto;
    if (estado.q) {
      texto = estado.total === 1
        ? '1 resultado para «' + estado.q + '»'
        : num(estado.total) + ' resultados para «' + estado.q + '»';
      if (temFiltro()) { texto += ' ' + descricaoFiltro(); }
    } else if (temFiltro()) {
      texto = num(estado.total) + (estado.total === 1 ? ' ditado ' : ' ditados ') + descricaoFiltro();
    } else {
      texto = num(estado.total) + (estado.total === 1 ? ' ditado no historico' : ' ditados no historico');
    }
    el.textContent = texto;
    return texto;
  }

  function carregar(novaConsulta) {
    if (estado.carregando || (estado.fim && !novaConsulta)) { return; }
    if (novaConsulta) {
      estado.pagina = 0;
      estado.fim = false;
      estado.aberto = null;
      contexto.dia = null;
      $('fim-lista').classList.add('oculta');
      mostrarEsqueleto();
    }
    estado.carregando = true;
    estado.pagina += 1;
    var minha = ++estado.geracao;

    pedir(montarUrl()).then(function (dado) {
      if (minha !== estado.geracao) { return; }
      estado.carregando = false;
      estado.total = dado.total || 0;

      if (estado.pagina === 1) {
        limpar(lista);
        contexto.dia = null;
        var texto = atualizarContagem();
        if (!dado.itens || !dado.itens.length) {
          estado.fim = true;
          mostrarNada();
          return;
        }
        anunciar(texto);
      }
      acrescentar(dado.itens || []);
      if (!dado.itens || dado.itens.length < estado.porPagina) {
        estado.fim = true;
        if (estado.total > estado.porPagina) { $('fim-lista').classList.remove('oculta'); }
      }
      conferirSentinela();
    }).catch(function (erro) {
      if (minha !== estado.geracao) { return; }
      estado.carregando = false;
      estado.fim = true;
      if (String(erro && erro.message) === 'sessao') { return; }
      if (estado.pagina === 1) {
        mostrarVazio(
          'Nao consegui abrir o historico',
          'O painel nao conseguiu ler o banco de ditados. O ditado em si nao e' +
          ' afetado - o botao flutuante continua funcionando normalmente.',
          'Tentar de novo', function () { carregar(true); },
          '%LOCALAPPDATA%\\BotaoWispr\\history.db'
        );
      }
    });
  }

  function mostrarNada() {
    if (estado.q) {
      var texto = temFiltro()
        ? ('Nada com «' + estado.q + '» ' + descricaoFiltro() + '.')
        : ('Nenhum dos ' + num(estado.totalGeral) +
           ' ditados tem essa palavra. A busca ignora acentos e maiusculas.');
      if (temFiltro()) {
        /* quase sempre e' o filtro que esta sobrando, nao a palavra */
        mostrarVazio('Nada com «' + estado.q + '»', texto, 'Limpar filtros', function () {
          limparFiltros();
        });
      } else {
        mostrarVazio('Nada com «' + estado.q + '»', texto, 'Limpar busca', function () {
          $('busca').value = '';
          estado.q = '';
          atualizarChip();
          carregar(true);
          $('busca').focus();
        });
      }
      anunciar('Nenhum resultado para ' + estado.q);
    } else if (temFiltro()) {
      mostrarVazio('Nenhum ditado ' + descricaoFiltro(),
        'Nada foi ditado com esse filtro. Tire o filtro para ver o historico inteiro.',
        'Limpar filtros', limparFiltros);
    } else {
      mostrarVazio('Nenhum ditado ainda',
        'Aperte o botao do microfone e fale. O que voce disser aparece aqui.');
    }
  }

  /* rolagem infinita: 60 por vez, sentinela no fim */
  var observador = null;
  function conferirSentinela() {
    if (!observador) { return; }
    /* re-observa: se a sentinela continua visivel, carrega a proxima pagina */
    observador.unobserve($('sentinela'));
    if (!estado.fim) { observador.observe($('sentinela')); }
  }

  function ligarSentinela() {
    if (!window.IntersectionObserver) { return; }
    observador = new IntersectionObserver(function (entradas) {
      if (entradas[0] && entradas[0].isIntersecting && !estado.carregando && !estado.fim) {
        carregar(false);
      }
    }, { rootMargin: '600px 0px' });
    observador.observe($('sentinela'));
  }

  /* =================================================================
     7. HISTORICO - busca e filtros
     ================================================================= */

  function atualizarChip() {
    var campo = $('busca'), chip = $('chip-busca');
    var focado = document.activeElement === campo;
    chip.textContent = (focado || campo.value) ? 'esc' : '/';
  }

  function marcarFiltros() {
    $('filtro-dia-caixa').classList.toggle('filtro-ativo', !!estado.modoDia);
    $('filtro-data-caixa').classList.toggle('oculta', estado.modoDia !== 'outro');
    $('filtro-data-caixa').classList.toggle('filtro-ativo', estado.modoDia === 'outro' && !!estado.data);
    $('filtro-app-caixa').classList.toggle('filtro-ativo', !!estado.app);
    var ativos = (estado.modoDia ? 1 : 0) + (estado.app ? 1 : 0);
    $('limpar-filtros').classList.toggle('oculta', ativos < 2);
  }

  function limparFiltros() {
    estado.modoDia = '';
    estado.data = '';
    estado.app = '';
    $('filtro-dia').value = '';
    $('filtro-data').value = '';
    $('filtro-app').value = '';
    guardar('modoDia', '');
    guardar('data', '');
    guardar('app', '');
    marcarFiltros();
    carregar(true);
  }

  function ligarBusca() {
    var campo = $('busca'), timer = null;
    campo.addEventListener('input', function () {
      atualizarChip();
      clearTimeout(timer);
      timer = setTimeout(function () {
        var v = campo.value.trim();
        if (v === estado.q) { return; }
        estado.q = v;
        carregar(true);
      }, 180);
    });
    campo.addEventListener('focus', atualizarChip);
    campo.addEventListener('blur', atualizarChip);

    $('filtro-dia').addEventListener('change', function () {
      estado.modoDia = this.value;
      guardar('modoDia', estado.modoDia);
      if (estado.modoDia === 'outro') {
        marcarFiltros();
        $('filtro-data').focus();
        if (!estado.data) { return; }
      }
      marcarFiltros();
      carregar(true);
    });

    $('filtro-data').addEventListener('change', function () {
      estado.data = this.value;
      guardar('data', estado.data);
      marcarFiltros();
      carregar(true);
    });

    $('filtro-app').addEventListener('change', function () {
      estado.app = this.value;
      guardar('app', estado.app);
      marcarFiltros();
      carregar(true);
    });

    $('limpar-filtros').addEventListener('click', limparFiltros);
  }

  function carregarApps() {
    pedir('/api/apps').then(function (apps) {
      var sel = $('filtro-app');
      (apps || []).forEach(function (a) {
        /* bundle vazio ("Sem app em foco") nao da pra filtrar: a rota usa
           app=<bundle> e o vazio e' indistinguivel de "sem filtro". Melhor
           nao oferecer do que oferecer um filtro que nao faz nada. */
        if (!a.bundle) { return; }
        var op = document.createElement('option');
        op.value = a.bundle;
        op.textContent = (a.nome || a.bundle) + '  ·  ' + num(a.n);
        op.dataset.nome = a.nome || a.bundle;
        sel.appendChild(op);
      });
      if (estado.app) { sel.value = estado.app; }
      if (sel.value !== estado.app) {   /* app salvo sumiu do historico */
        estado.app = '';
        guardar('app', '');
        carregar(true);
      } else if (estado.app) {
        atualizarContagem();            /* agora da pra escrever o nome bonito */
      }
      marcarFiltros();
    }).catch(function () {});
  }

  function carregarResumo() {
    pedir('/api/resumo').then(function (r) {
      var regua = $('regua');
      estado.totalGeral = r.total || 0;
      limpar(regua);
      var itens = [
        [num(r.total), r.total === 1 ? 'ditado' : 'ditados'],
        [num(r.hoje), 'hoje'],
        [num(r.sete_dias), 'em 7 dias'],
        [falado(r.minutos_audio), 'falados']
      ];
      if (r.primeiro_dia) { itens.push([ddmm(r.primeiro_dia), 'primeiro ditado']); }
      itens.forEach(function (par) {
        var caixa = criar('span');
        caixa.appendChild(criar('b', null, par[0]));
        caixa.appendChild(document.createTextNode(par[1]));
        regua.appendChild(caixa);
      });
      var d = $('filtro-data');
      if (r.primeiro_dia) { d.min = r.primeiro_dia; }
      if (r.ultimo_dia) { d.max = r.ultimo_dia; }
      medirTopo();
    }).catch(function () {});
  }

  function falado(minutos) {
    var m = Math.round(minutos || 0);
    if (m < 60) { return m + ' min'; }
    return Math.floor(m / 60) + ' h ' + zero(m % 60) + ' min';
  }

  /* =================================================================
     7b. EXPORTAR - o history.db e' uma janela de 30 dias; isto salva
     o que expiraria. A caixa diz QUANTOS ditados vao no arquivo antes
     de baixar (estado.total: e' o total da MESMA consulta que a tela
     esta mostrando, entao busca e filtros valem automaticamente).
     ================================================================= */

  var abrirCaixaExportar = null;   /* preenchido por ligarExportar */
  var fecharCaixaExportar = null;

  function urlExportar(formato) {
    var p = ['formato=' + formato];
    if (estado.q) { p.push('q=' + encodeURIComponent(estado.q)); }
    var d = diaEscolhido();
    if (d) { p.push('dia=' + encodeURIComponent(d)); }
    if (estado.app) { p.push('app=' + encodeURIComponent(estado.app)); }
    return '/api/exportar?' + p.join('&');
  }

  function baixarExportacao(formato, aoTerminar) {
    var nome = 'ditados-' + (diaEscolhido() || hojeDia()) + '.' + formato;
    fetch(urlExportar(formato), {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: cabecalhosSessao()
    }).then(function (r) {
      if (r.status === 403) {
        mostrarBloqueio();
        throw new Error('sessao');
      }
      if (!r.ok) { throw new Error('http ' + r.status); }
      var cd = r.headers.get('Content-Disposition') || '';
      var m = /filename="([^"]+)"/.exec(cd);
      if (m) { nome = m[1]; }
      return r.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      /* o revoke imediato cancelaria o download em andamento */
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      aoTerminar(null, nome);
    }).catch(function (erro) {
      aoTerminar(erro, nome);
    });
  }

  function ligarExportar() {
    var btn = $('btn-exportar'), caixa = $('exportar-caixa');
    var info = $('exportar-info'), resposta = $('exportar-resposta');
    var bMd = $('btn-exportar-md'), bTxt = $('btn-exportar-txt');
    var bNao = $('btn-exportar-cancelar');
    var baixando = false;

    function fraseDoAlvo() {
      var n = estado.total;
      if (!n) { return 'Nenhum ditado com a busca/filtros atuais - nada para exportar.'; }
      var t = 'Vao ' + num(n) + (n === 1 ? ' ditado' : ' ditados') + ' no arquivo';
      var contexto = [];
      if (estado.q) { contexto.push('com «' + estado.q + '»'); }
      if (temFiltro()) { contexto.push(descricaoFiltro()); }
      t += contexto.length ? (' (' + contexto.join(', ') + ').')
                           : ' - o historico inteiro.';
      return t;
    }

    function abrir() {
      info.textContent = fraseDoAlvo();
      var tem = estado.total > 0;
      bMd.disabled = !tem;
      bTxt.disabled = !tem;
      resposta.classList.add('oculta');
      caixa.classList.remove('oculta');
      anunciar(info.textContent);
    }

    function fechar(devolverFoco) {
      caixa.classList.add('oculta');
      if (devolverFoco) { btn.focus(); }
    }
    abrirCaixaExportar = abrir;
    fecharCaixaExportar = function () {
      if (caixa.classList.contains('oculta') || baixando) { return false; }
      fechar(true);
      return true;
    };

    btn.addEventListener('click', function () {
      if (caixa.classList.contains('oculta')) { abrir(); } else { fechar(true); }
    });
    bNao.addEventListener('click', function () { fechar(true); });

    function disparar(formato, botao) {
      if (baixando) { return; }
      baixando = true;
      var n = estado.total;
      var original = botao.textContent;
      bMd.disabled = true;
      bTxt.disabled = true;
      botao.textContent = 'Preparando...';
      baixarExportacao(formato, function (erro, nome) {
        baixando = false;
        botao.textContent = original;
        bMd.disabled = false;
        bTxt.disabled = false;
        resposta.classList.remove('oculta', 'st-ruim');
        if (erro) {
          if (String(erro.message) === 'sessao') { return; }
          resposta.classList.add('st-ruim');
          resposta.textContent = 'Nao consegui gerar o arquivo. Tente de novo.';
        } else {
          resposta.textContent = 'Baixado: ' + nome + ' (' + num(n) +
            (n === 1 ? ' ditado' : ' ditados') + '). Esta na pasta Downloads.';
        }
        anunciar(resposta.textContent);
      });
    }

    bMd.addEventListener('click', function () { disparar('md', bMd); });
    bTxt.addEventListener('click', function () { disparar('txt', bTxt); });
  }

  /* =================================================================
     7c. INSIGHTS - numeros agregados do banco. Nada inventado: tudo
     que aparece aqui saiu de SELECT no history.db (rota /api/insights).
     Grafico em divs puras (CSP bloqueia qualquer biblioteca); altura
     das barras via style do CSSOM, que a CSP permite (style= inline
     na marcacao e' que e' proibido).
     ================================================================= */

  function tile(valor, rotulo, titulo) {
    var t = criar('div', 'tile');
    var b = criar('b', null, valor);
    t.appendChild(b);
    t.appendChild(criar('span', null, rotulo));
    if (titulo) { t.title = titulo; }
    return t;
  }

  function desenharTiles(d) {
    var raiz = $('ins-tiles');
    limpar(raiz);
    raiz.appendChild(tile(falado((d.fala_total_s || 0) / 60), 'de fala transcrita',
      umaCasa((d.fala_total_s || 0) / 60) + ' minutos'));
    raiz.appendChild(tile(mss(d.media_s), 'media por ditado'));
    raiz.appendChild(tile(d.ppm ? num(d.ppm) : '--', 'palavras por minuto',
      num(d.palavras) + ' palavras no total'));
    raiz.appendChild(tile(num(d.palavras_hoje || 0), 'palavras ditadas hoje'));
    var pico = d.hora_pico;
    raiz.appendChild(tile(pico ? (pico.hora + 'h-' + ((pico.hora + 1) % 24) + 'h') : '--',
      'horario em que voce mais dita',
      pico ? (num(pico.n) + ' ditados nessa faixa') : ''));
  }

  function desenharGraficoDias(d) {
    var raiz = $('grafico-dias');
    limpar(raiz);
    var dias = d.dias || [];
    var max = 0, i;
    for (i = 0; i < dias.length; i++) { if (dias[i].n > max) { max = dias[i].n; } }

    var barras = criar('div', 'g-barras');
    var rotulos = criar('div', 'g-rotulos');
    var rotulouMax = false;
    dias.forEach(function (x, idx) {
      var col = criar('div', 'g-col');
      col.title = ddmm(x.dia) + ' - ' + (x.n === 1 ? '1 ditado' : x.n + ' ditados') +
        ' - ' + num(x.palavras || 0) + ' palavras';
      var barra = criar('i', 'g-barra');
      barra.style.height = (max > 0 ? (x.n / max) * 100 : 0).toFixed(1) + '%';
      if (x.n > 0) { barra.classList.add('g-tem'); }
      col.appendChild(barra);
      /* rotulo direto SO no pico (selectivo; o resto fica no title) */
      if (!rotulouMax && max > 0 && x.n === max) {
        col.appendChild(criar('span', 'g-max', num(x.n)));
        rotulouMax = true;
      }
      barras.appendChild(col);
      var mostrar = (idx === 0 || idx === dias.length - 1 || idx % 5 === 0);
      rotulos.appendChild(criar('span', 'g-rot', mostrar ? ddmm(x.dia) : ''));
    });
    raiz.appendChild(barras);
    raiz.appendChild(rotulos);
    raiz.setAttribute('role', 'img');
    raiz.setAttribute('aria-label', 'Grafico de barras: ditados por dia nos ultimos ' +
      (d.janela_dias || 30) + ' dias. Total ' + num(d.dias_total) +
      (max > 0 ? ('. Pico de ' + num(max) + ' num dia.') : '.'));
    raiz.setAttribute('aria-describedby', 'grafico-dias-dados');
    $('grafico-dias-dados').textContent = dias.map(function (x) {
      return ddmm(x.dia) + ': ' + num(x.n) + (x.n === 1 ? ' ditado' : ' ditados');
    }).join('. ');

    $('ins-dias-resumo').textContent = num(d.dias_total) +
      (d.dias_total === 1 ? ' ditado' : ' ditados') + ' nos ultimos ' +
      (d.janela_dias || 30) + ' dias' +
      (max > 0 ? (' - pico de ' + num(max) + ' num dia') : '') + '.';
  }

  function linhaMedidor(nome, n, pct, zerado) {
    var l = criar('div', 'medidor-linha' + (zerado ? ' medidor-zero' : ''));
    var elNome = criar('span', 'medidor-nome', nome);
    elNome.title = nome;
    l.appendChild(elNome);
    var trilho = criar('span', 'medidor-trilho');
    trilho.setAttribute('aria-hidden', 'true');
    var fill = criar('i');
    fill.style.width = Math.max(0, Math.min(100, pct || 0)).toFixed(1) + '%';
    trilho.appendChild(fill);
    l.appendChild(trilho);
    l.appendChild(criar('span', 'medidor-valor',
      zerado ? 'nunca usado - 0' : (num(n) + ' - ' + umaCasa(pct) + '%')));
    return l;
  }

  function desenharApps(d) {
    var raiz = $('ins-apps');
    limpar(raiz);
    (d.apps || []).forEach(function (a) {
      raiz.appendChild(linhaMedidor(a.nome || a.bundle, a.n, a.pct, false));
    });
    if (d.apps_outros && d.apps_outros.n > 0) {
      raiz.appendChild(linhaMedidor('outros apps', d.apps_outros.n, d.apps_outros.pct, false));
    }
    if (!raiz.firstChild) {
      raiz.appendChild(criar('p', 'sem-aviso', 'Nenhum ditado no banco ainda.'));
    }
  }

  function desenharPerfis(d) {
    var raiz = $('ins-perfis');
    var zeroCaixa = $('ins-perfis-zero');
    limpar(raiz);
    limpar(zeroCaixa);
    var zerados = [];
    (d.perfis || []).forEach(function (p) {
      var zerado = (p.n === 0);
      if (zerado) { zerados.push(p.perfil); }
      raiz.appendChild(linhaMedidor('perfil ' + p.perfil, p.n, p.pct, zerado));
    });
    /* ZERO explicito e' a informacao valiosa: foi um "prontuario: 0" que
       revelou o dicionario medico configurado no perfil errado (31/07) */
    if (zerados.length && d.total > 0) {
      var frase = (zerados.length === 1)
        ? ('O perfil ' + zerados[0] + ' NUNCA foi usado: 0 dos ' + num(d.total) +
           ' ditados. Se voce achava que usava, tem configuracao no lugar errado.')
        : ('Os perfis ' + zerados.join(' e ') + ' NUNCA foram usados: 0 dos ' +
           num(d.total) + ' ditados. Se voce achava que usava algum deles, tem ' +
           'configuracao no lugar errado.');
      zeroCaixa.appendChild(criar('p', 'ins-zero', frase));
    }
  }

  function desenharInsights(d) {
    $('ins-carimbo').textContent = 'calculado as ' + (d.gerado_em || '--:--:--') +
      ' sobre ' + num(d.total) + (d.total === 1 ? ' ditado' : ' ditados');
    desenharTiles(d);
    desenharGraficoDias(d);
    desenharApps(d);
    desenharPerfis(d);
  }

  function insightsIndisponivel() {
    $('ins-carimbo').textContent = 'sem leitura';
    var raiz = $('ins-tiles');
    limpar(raiz);
    var p = criar('p', 'sem-aviso',
      'Nao consegui ler o banco de ditados agora. O ditado em si nao e afetado - ' +
      'troque de aba e volte para tentar de novo.');
    raiz.appendChild(p);
  }

  function buscarInsights() {
    pedir('/api/insights').then(desenharInsights).catch(function (erro) {
      if (String(erro && erro.message) === 'sessao') { return; }
      insightsIndisponivel();
    });
  }

  function ligarInsights() {
    $('btn-ir-exportar').addEventListener('click', function () {
      trocarAba('historico');
      window.scrollTo(0, 0);
      if (abrirCaixaExportar) { abrirCaixaExportar(); }
      $('btn-exportar').focus();
    });
  }

  /* =================================================================
     8. SAUDE
     ================================================================= */

  var GRUPOS = [
    { chave: 'ditado',  nome: 'O ditado' },
    { chave: 'motor',   nome: 'O motor' },
    { chave: 'memoria', nome: 'A memoria' }
  ];

  var ORDEM = { ok: 0, neutro: 1, aviso: 2, ruim: 3 };
  var PALAVRA_ESTADO = {
    rodando: 'no ar', parado: 'parado',
    descarregado: 'descarregado', 'sob demanda': 'sob demanda'
  };
  /* sonda que falhou NAO e' vermelho. "nao sei" e "quebrado" sao coisas diferentes */
  var SEM_LEITURA = /(sem leitura|nao deu|não deu|expirou|timeout|time out|desconhecid|nao consegui|não consegui|erro ao|falha ao ler|indisponivel|indisponível)/i;

  function estadoBloco(b) {
    if (!b) { return 'neutro'; }
    if (b.ok) { return 'ok'; }
    return SEM_LEITURA.test(String(b.detalhe || '')) ? 'neutro' : 'ruim';
  }

  /* cor da LINHA do agente. O warmup e' one-shot: sem PID e ultimo exit 0 =
     concluido, nao morto. Ele fica cinza ("ja rodou no boot"), nunca vermelho. */
  function estadoAgente(a) {
    if (!a) { return 'neutro'; }
    if (a.ok) { return a.estado === 'rodando' ? 'ok' : 'neutro'; }
    if (a.estado === 'parado' || a.estado === 'descarregado') { return 'ruim'; }
    return 'aviso';
  }

  /* peso do agente no VEREDITO do grupo: quem esta ok nao rebaixa o grupo -
     senao o warmup (cinza por natureza) faria o grupo inteiro dizer
     "sem leitura" todo dia, que e' o alarme falso classico deste painel. */
  function pesoAgente(a) {
    return (a && a.ok) ? 'ok' : estadoAgente(a);
  }

  function pior(lista) {
    var p = 'ok', i;
    for (i = 0; i < lista.length; i++) {
      if (ORDEM[lista[i]] > ORDEM[p]) { p = lista[i]; }
    }
    return p;
  }

  /* PORTE WINDOWS: os labels deixaram de ser os do launchd
     (com.jorgeperes.ditado-mic-server) e passaram a ser os nomes das tarefas
     do Agendador (DitadoMic-Servidor). Dai o /i (o "G" de Guardiao
     e maiusculo) e o 'servidor', que em ingles era 'server'. */
  function grupoDoAgente(label) {
    var l = String(label || '');
    if (/guardiao/i.test(l)) { return 'memoria'; }
    if (/(server|servidor|keepwarm|keep-warm|warmup|coreml)/i.test(l)) { return 'motor'; }
    return 'ditado';
  }

  var caixas = {};   /* chave do grupo -> nos reaproveitados entre atualizacoes */

  function montarGrupos() {
    var raiz = $('grupos');
    limpar(raiz);
    caixas = {};
    GRUPOS.forEach(function (g) {
      var sec = criar('section', 'grupo');
      sec.dataset.chave = g.chave;

      var h3 = criar('h3');
      var topo = criar('button', 'grupo-topo');
      topo.type = 'button';
      topo.setAttribute('aria-expanded', 'false');
      topo.setAttribute('aria-controls', 'g-' + g.chave);

      var ponto = criar('span', 'ponto ponto-neutro');
      ponto.setAttribute('aria-hidden', 'true');
      topo.appendChild(ponto);

      var info = criar('span', 'grupo-info');
      info.appendChild(criar('span', 'grupo-nome', g.nome));
      var frase = criar('span', 'grupo-frase', 'verificando');
      info.appendChild(frase);
      topo.appendChild(info);

      var contador = criar('span', 'grupo-contador', '--');
      topo.appendChild(contador);

      var chev = criar('span', 'chevron');
      chev.setAttribute('aria-hidden', 'true');
      chev.appendChild(seta());
      topo.appendChild(chev);

      h3.appendChild(topo);
      sec.appendChild(h3);

      var corpo = criar('div', 'grupo-corpo');
      corpo.id = 'g-' + g.chave;
      corpo.hidden = true;
      var linhas = criar('div', 'linhas');
      corpo.appendChild(linhas);
      var extra = criar('div', 'extra');
      corpo.appendChild(extra);
      sec.appendChild(corpo);

      topo.addEventListener('click', function () {
        var aberto = topo.getAttribute('aria-expanded') === 'true';
        topo.setAttribute('aria-expanded', aberto ? 'false' : 'true');
        if (aberto) {
          animarAltura(corpo, corpo.scrollHeight, 0, function () { corpo.hidden = true; });
        } else {
          corpo.hidden = false;
          animarAltura(corpo, 0, corpo.scrollHeight);
        }
      });

      raiz.appendChild(sec);
      caixas[g.chave] = {
        sec: sec, topo: topo, ponto: ponto, frase: frase, contador: contador,
        linhas: linhas, extra: extra, corpo: corpo, cache: {}, estado: null
      };
    });
  }

  function seta() { return molde('molde-seta'); }

  /* linha de componente reaproveitada: atualiza textContent, nao recria o no */
  function linha(caixa, chave, nome, comBarra) {
    var l = caixa.cache[chave];
    if (!l) {
      var raiz = criar('div', 'linha' + (comBarra ? ' linha-swap' : ''));
      var elNome = criar('span', 'linha-nome', nome);
      raiz.appendChild(elNome);
      var trilho = null, fill = null;
      if (comBarra) {
        trilho = criar('span', 'swap-trilho');
        trilho.setAttribute('aria-hidden', 'true');
        fill = criar('i');
        trilho.appendChild(fill);
        raiz.appendChild(trilho);
      }
      var elEstado = criar('span', 'linha-estado', '');
      raiz.appendChild(elEstado);
      var elDetalhe = criar('span', 'linha-detalhe', '');
      raiz.appendChild(elDetalhe);
      caixa.linhas.appendChild(raiz);
      l = { raiz: raiz, nome: elNome, estado: elEstado, detalhe: elDetalhe, trilho: trilho, fill: fill };
      caixa.cache[chave] = l;
    }
    l.nome.textContent = nome;
    l.visto = true;
    return l;
  }

  function porStatus(el, st) {
    el.classList.remove('st-ok', 'st-aviso', 'st-ruim', 'st-neutro');
    el.classList.add('st-' + st);
  }

  function pontoClasse(el, st, grande) {
    el.className = 'ponto' + (grande ? ' ponto-grande' : '') + ' ponto-' +
      (st === 'ok' ? 'ok' : st === 'ruim' ? 'ruim' : st === 'aviso' ? 'aviso' : 'neutro');
  }

  function preencherLinha(caixa, chave, nome, st, palavra, detalhe) {
    var l = linha(caixa, chave, nome, false);
    l.estado.textContent = palavra;
    porStatus(l.estado, st);
    l.detalhe.textContent = detalhe || '';
    /* o detalhe pode ser truncado em coluna estreita: o title mostra inteiro */
    if (detalhe) { l.detalhe.title = detalhe; } else { l.detalhe.removeAttribute('title'); }
    return l;
  }

  function desenharSaude(s) {
    estado.saude = s;

    /* ---- separa os componentes por grupo ---- */
    var porGrupo = { ditado: [], motor: [], memoria: [] };
    (s.agentes || []).forEach(function (a) { porGrupo[grupoDoAgente(a.label)].push(a); });

    var stServer = estadoBloco(s.server);
    /* PORTE WINDOWS: no Mac este bloco era o Core ML (que nao existe aqui);
       virou a contagem de processos Python (tasklist). */
    var stProc = estadoBloco(s.processos);
    var stKeep = estadoBloco(s.keepwarm);
    var stSwap = s.swap ? (s.swap.ok ? 'ok' : 'aviso') : 'neutro';
    if (s.swap && !s.swap.ok && s.swap.pct >= 92) { stSwap = 'ruim'; }

    var estados = {
      ditado: porGrupo.ditado.map(pesoAgente),
      motor: porGrupo.motor.map(pesoAgente).concat([stServer, stProc, stKeep]),
      memoria: porGrupo.memoria.map(pesoAgente).concat([stSwap])
    };

    /* ---- grupo: O ditado ---- */
    var c = caixas.ditado;
    marcarNaoVistos(c);
    porGrupo.ditado.forEach(function (a) { desenharAgente(c, a); });
    limparNaoVistos(c);
    if (!estados.ditado.length) { estados.ditado = ['neutro']; }
    var stDitado = pior(estados.ditado);
    aplicarGrupo(c, stDitado, fraseDitado(porGrupo.ditado, stDitado),
                 contagem(estados.ditado, stDitado));

    /* ---- grupo: O motor ---- */
    c = caixas.motor;
    marcarNaoVistos(c);
    preencherLinha(c, 'server', 'Whisper-server', stServer,
      stServer === 'ok' ? 'responde' : (stServer === 'neutro' ? 'sem leitura' : 'fora do ar'),
      detalheServer(s.server, stServer));
    preencherLinha(c, 'processos', 'Processos Python', stProc,
      stProc === 'ok' ? 'no ar' : (stProc === 'neutro' ? 'sem leitura' : 'nenhum'),
      (s.processos && s.processos.detalhe) || '');
    /* "Ultimo ping" e' a SONDA do keep-warm; o agente launchd homonimo entra
       logo abaixo com o rotulo que a API mandar - dois nomes iguais confundem */
    preencherLinha(c, 'keepwarm', 'Ultimo ping', stKeep,
      stKeep === 'ok' ? 'aquecendo' : (stKeep === 'neutro' ? 'sem leitura' : 'parado'),
      detalheKeep(s.keepwarm));
    porGrupo.motor.forEach(function (a) { desenharAgente(c, a); });
    limparNaoVistos(c);
    var stMotor = pior(estados.motor);
    aplicarGrupo(c, stMotor, fraseMotor(s, stServer, stMotor),
                 contagem(estados.motor, stMotor));

    /* ---- grupo: A memoria ---- */
    c = caixas.memoria;
    marcarNaoVistos(c);
    desenharSwap(c, s.swap, stSwap);
    porGrupo.memoria.forEach(function (a) { desenharAgente(c, a); });
    limparNaoVistos(c);
    desenharIncidentes(c, s.incidentes || []);
    var stMem = pior(estados.memoria);
    aplicarGrupo(c, stMem, fraseMemoria(s, stSwap), contagem(estados.memoria, stMem));

    /* ---- veredito ---- */
    var geral = pior([stDitado, stMotor, stMem]);
    if (stDitado === 'neutro' && stMotor === 'neutro' && stMem === 'neutro') { geral = 'nada'; }
    if (geral !== 'ruim' && (s.alertas || []).length && geral !== 'nada') {
      geral = ORDEM[geral] > ORDEM.aviso ? geral : 'aviso';
    }
    aplicarVeredito(geral, s, stServer);

    /* ---- abre sozinho o grupo que piorou; o pior ocupa a linha inteira ---- */
    GRUPOS.forEach(function (g) {
      var cx = caixas[g.chave];
      var st = g.chave === 'ditado' ? stDitado : (g.chave === 'motor' ? stMotor : stMem);
      var piorou = cx.estado !== null && ORDEM[st] > ORDEM[cx.estado];
      var primeiraVez = cx.estado === null;
      cx.sec.classList.toggle('largo', st === geral && ORDEM[st] >= ORDEM.aviso);
      if ((piorou || (primeiraVez && ORDEM[st] >= ORDEM.aviso)) &&
          cx.topo.getAttribute('aria-expanded') !== 'true') {
        cx.topo.setAttribute('aria-expanded', 'true');
        cx.corpo.hidden = false;
      }
      cx.estado = st;
    });

    $('carimbo').textContent = 'verificado as ' + (s.gerado_em || '--:--:--');
  }

  function marcarNaoVistos(caixa) {
    Object.keys(caixa.cache).forEach(function (k) { caixa.cache[k].visto = false; });
  }
  function limparNaoVistos(caixa) {
    Object.keys(caixa.cache).forEach(function (k) {
      if (!caixa.cache[k].visto) {
        if (caixa.cache[k].raiz.parentNode) {
          caixa.cache[k].raiz.parentNode.removeChild(caixa.cache[k].raiz);
        }
        delete caixa.cache[k];
      }
    });
  }

  function desenharAgente(caixa, a) {
    var st = estadoAgente(a);
    var palavra = PALAVRA_ESTADO[a.estado] || a.estado || 'sem leitura';
    var det = a.detalhe || '';
    if (!det && a.pid) { det = 'pid ' + a.pid; }
    preencherLinha(caixa, 'ag:' + a.label, a.rotulo || a.label, st, palavra, det);
  }

  function desenharSwap(caixa, swap, st) {
    var l = linha(caixa, 'swap', 'Swap', true);
    var pct = swap ? Math.max(0, Math.min(100, swap.pct || 0)) : 0;
    if (l.fill) { l.fill.style.width = pct.toFixed(1) + '%'; }
    if (l.trilho) {
      l.trilho.classList.remove('st-ok', 'st-ruim');
      if (pct < 60) { l.trilho.classList.add('st-ok'); }
      if (pct > 92) { l.trilho.classList.add('st-ruim'); }
    }
    l.estado.textContent = swap ? (umaCasa(pct) + '% em uso') : 'sem leitura';
    porStatus(l.estado, st);
    l.detalhe.textContent = swap
      ? (num(swap.usado_mb) + ' de ' + num(swap.total_mb) + ' MB')
      : '';
  }

  function desenharIncidentes(caixa, incidentes) {
    limpar(caixa.extra);
    caixa.extra.appendChild(criar('p', 'avisos-titulo', 'Ultimos avisos'));
    if (!incidentes.length) {
      caixa.extra.appendChild(criar('p', 'sem-aviso', 'Nenhum aviso recente.'));
      return;
    }
    incidentes.slice(0, 5).forEach(function (inc) {
      var l = criar('div', 'incidente');
      l.appendChild(criar('span', null, inc.quando || ''));
      /* a mensagem ja vem escrita em portugues humano pelo guardiao: verbatim */
      l.appendChild(criar('p', null, inc.mensagem || ''));
      caixa.extra.appendChild(l);
    });
  }

  function contagem(estados, st) {
    if (st === 'ok') {
      return estados.length + ' de ' + estados.length;
    }
    if (st === 'neutro') { return 'sem leitura'; }
    if (st === 'ruim') { return 'parado'; }
    return 'atencao';
  }

  function aplicarGrupo(caixa, st, frase, contador) {
    pontoClasse(caixa.ponto, st, false);
    caixa.frase.textContent = frase;
    caixa.contador.textContent = contador;
    porStatus(caixa.contador, st);
  }

  function detalheServer(server, st) {
    if (!server) { return ''; }
    if (st === 'ok') {
      return 'porta 7777' + (server.ms !== null && server.ms !== undefined
        ? ' · ' + umaCasa(server.ms) + ' ms' : '');
    }
    return server.detalhe || '';
  }

  function detalheKeep(k) {
    if (!k) { return ''; }
    if (k.ha_s !== null && k.ha_s !== undefined) {
      var m = Math.round(k.ha_s / 60);
      return 'ultimo ping ha ' + (k.ha_s < 90 ? k.ha_s + ' s' : m + ' min');
    }
    return k.detalhe || '';
  }

  function fraseDitado(agentes, st) {
    if (st === 'ok') { return 'No ar.'; }
    if (st === 'neutro') { return 'Nao deu pra verificar.'; }
    var quebrados = agentes.filter(function (a) { return pesoAgente(a) !== 'ok'; })
                           .map(function (a) { return a.rotulo || a.label; });
    return quebrados.length ? (quebrados.join(', ') + ' com problema.') : 'Com ressalva.';
  }

  function fraseMotor(s, stServer, st) {
    if (stServer === 'ruim') { return 'Nao responde na porta 7777.'; }
    if (st === 'neutro') { return 'Nao deu pra verificar.'; }
    var base = 'Quente';
    if (s.server && s.server.ms !== null && s.server.ms !== undefined) {
      base += '. Responde em ' + umaCasa(s.server.ms) + ' ms';
    }
    return st === 'ok' ? (base + '.') : (base + ', mas com ressalva.');
  }

  function maiusc(t) { return t ? (t.charAt(0).toUpperCase() + t.slice(1)) : t; }

  function fraseMemoria(s, stSwap) {
    var partes = [];
    if (s.swap) { partes.push(umaCasa(s.swap.pct) + '% do swap em uso'); }
    else { partes.push('Sem leitura do swap'); }
    var n = (s.incidentes || []).length;
    partes.push(maiusc(n === 0 ? 'nenhum aviso recente'
                                : (n === 1 ? '1 aviso recente' : n + ' avisos recentes')));
    return partes.join('. ') + '.';
  }

  function aplicarVeredito(geral, s, stServer) {
    var faixa = $('faixa-veredito');
    var titulo, sub, curto, cor;

    if (geral === 'nada') {
      cor = 'neutro';
      titulo = 'Nao consegui ler o estado do sistema';
      sub = 'Isso e problema do painel, nao do ditado - o botao flutuante continua ' +
            'funcionando normalmente.';
      curto = 'Sem leitura do sistema';
    } else if (geral === 'ruim') {
      cor = 'ruim';
      if (stServer === 'ruim') {
        titulo = 'O motor de transcricao esta fora do ar';
        sub = 'O botao ainda grava, mas nada vai virar texto. Clique em reiniciar no ' +
              'botao flutuante, ou use o botao aqui embaixo.';
        curto = 'O ditado nao vai transcrever';
      } else {
        titulo = 'O ditado nao vai funcionar agora';
        sub = (s.alertas || []).join(' ') || 'Um dos agentes do ditado esta parado.';
        curto = 'O ditado nao vai transcrever';
      }
    } else if (geral === 'aviso') {
      cor = 'aviso';
      var alertas = (s.alertas || []);
      titulo = alertas.length === 1 ? 'Da pra ditar, com uma ressalva'
                                    : 'Da pra ditar, com ressalva';
      sub = alertas.join(' ') || 'Tudo funciona, mas tem algo pra saber.';
      curto = 'Da pra ditar, com ressalva';
    } else {
      cor = 'ok';
      titulo = 'Tudo pronto pra ditar';
      var ms = (s.server && s.server.ms !== null && s.server.ms !== undefined)
        ? ('Motor quente, respondeu em ' + umaCasa(s.server.ms) + ' ms.') : 'Motor quente.';
      var n = (s.incidentes || []).length;
      sub = ms + ' ' + (n ? (n === 1 ? '1 aviso recente.' : n + ' avisos recentes.')
                          : 'Nenhum aviso recente.');
      curto = 'Tudo pronto pra ditar';
    }

    faixa.classList.remove('st-aviso', 'st-ruim');
    if (cor === 'aviso') { faixa.classList.add('st-aviso'); }
    if (cor === 'ruim') { faixa.classList.add('st-ruim'); }
    pontoClasse($('faixa-ponto'), cor, true);
    $('faixa-titulo').textContent = titulo;
    $('faixa-sub').textContent = sub;

    pontoClasse($('veredito-ponto'), cor, false);
    if ($('veredito-texto').textContent !== curto) {
      $('veredito-texto').textContent = curto;
      anunciar('Estado do sistema: ' + curto);
    }
  }

  function saudeIndisponivel() {
    aplicarVeredito('nada', { alertas: [], incidentes: [] }, 'neutro');
    GRUPOS.forEach(function (g) {
      var c = caixas[g.chave];
      if (!c) { return; }
      pontoClasse(c.ponto, 'neutro', false);
      c.frase.textContent = 'Nao deu pra verificar.';
      c.contador.textContent = 'sem leitura';
      porStatus(c.contador, 'neutro');
    });
    $('carimbo').textContent = 'sem leitura';
  }

  function buscarSaude() {
    return pedir('/api/saude').then(desenharSaude).catch(function (erro) {
      if (String(erro && erro.message) === 'sessao') { return; }
      saudeIndisponivel();
    });
  }

  function ligarTimerSaude() {
    pararTimerSaude();
    if (estado.aba !== 'saude' || document.hidden) { return; }
    estado.timerSaude = setInterval(buscarSaude, 15000);
  }
  function pararTimerSaude() {
    if (estado.timerSaude) { clearInterval(estado.timerSaude); estado.timerSaude = null; }
  }

  /* ---------------- acoes da tela de saude ---------------- */

  var fecharConfirmacaoReiniciar = null;

  function ligarAcoes() {
    var btn = $('btn-reiniciar');
    var caixa = $('confirmar-reiniciar');
    var resposta = $('resposta-reiniciar');

    function esconderConfirmacao(devolverFoco) {
      caixa.classList.add('oculta');
      btn.classList.remove('oculta');
      if (devolverFoco) { btn.focus(); }
    }
    fecharConfirmacaoReiniciar = function () {
      if (caixa.classList.contains('oculta')) { return false; }
      esconderConfirmacao(true);
      return true;
    };

    /* nunca dispara com um clique so */
    btn.addEventListener('click', function () {
      btn.classList.add('oculta');
      caixa.classList.remove('oculta');
      $('btn-reiniciar-sim').focus();
    });
    $('btn-reiniciar-nao').addEventListener('click', function () {
      esconderConfirmacao(true);
    });
    $('btn-reiniciar-sim').addEventListener('click', function () {
      var sim = this;
      sim.disabled = true;
      sim.textContent = 'Reiniciando...';
      postar('/api/servidor/reiniciar', { confirmar: true }).then(function (r) {
        resposta.textContent = r.mensagem || (r.ok ? 'Servidor reiniciado.' : 'Nao consegui reiniciar.');
        resposta.classList.remove('oculta', 'st-ruim', 'st-ok');
        resposta.classList.add(r.ok ? 'st-ok' : 'st-ruim');
        anunciar(resposta.textContent);
        setTimeout(buscarSaude, 2500);
      }).catch(function () {
        resposta.textContent = 'Nao consegui falar com o painel para reiniciar o servidor.';
        resposta.classList.remove('oculta', 'st-ok');
        resposta.classList.add('st-ruim');
      }).then(function () {
        sim.disabled = false;
        sim.textContent = 'Sim, reiniciar';
        esconderConfirmacao(true);
      });
    });

    $('btn-sair').addEventListener('click', function () {
      var alvo = this;
      alvo.disabled = true;
      var fim = function () {
        pararTimerSaude();
        $('resposta-sair').textContent = 'Painel encerrado. Pode fechar esta janela.';
        $('resposta-sair').classList.remove('oculta');
        try { window.close(); } catch (e) {}
      };
      postar('/api/sair', {}).then(fim, fim);
    });
  }

  /* =================================================================
     9. ABAS, ATALHOS E ARRANQUE
     ================================================================= */

  var ABAS_ORDEM = ['historico', 'saude', 'insights'];
  var ABAS_NOME = { historico: 'Historico', saude: 'Saude', insights: 'Insights' };

  function trocarAba(qual, focarAba) {
    if (ABAS_ORDEM.indexOf(qual) === -1) { qual = 'historico'; }
    estado.aba = qual;
    guardar('aba', qual);
    ABAS_ORDEM.forEach(function (k) {
      $('aba-' + k).setAttribute('aria-selected', k === qual ? 'true' : 'false');
      $('aba-' + k).setAttribute('tabindex', k === qual ? '0' : '-1');
      $('tela-' + k).classList.toggle('oculta', k !== qual);
    });
    document.title = 'Botao Wispr - ' + ABAS_NOME[qual];
    if (qual === 'saude') { buscarSaude(); }
    if (qual === 'insights') { buscarInsights(); }
    ligarTimerSaude();
    if (focarAba) { $('aba-' + qual).focus(); }
  }

  function ligarAbas() {
    var abas = ABAS_ORDEM.map(function (k) { return $('aba-' + k); });
    abas.forEach(function (b, i) {
      b.addEventListener('click', function () { trocarAba(ABAS_ORDEM[i]); });
      b.addEventListener('keydown', function (ev) {
        var j = -1;
        if (ev.key === 'ArrowRight') { j = (i + 1) % ABAS_ORDEM.length; }
        if (ev.key === 'ArrowLeft') { j = (i + ABAS_ORDEM.length - 1) % ABAS_ORDEM.length; }
        if (j !== -1) {
          ev.preventDefault();
          abas[j].focus();
          trocarAba(ABAS_ORDEM[j]);
        }
      });
    });
    $('veredito-topo').addEventListener('click', function () {
      trocarAba('saude');
      $('aba-saude').focus();
    });
  }

  function ligarAtalhos() {
    document.addEventListener('keydown', function (ev) {
      var alvo = ev.target;
      var digitando = alvo && (alvo.tagName === 'INPUT' || alvo.tagName === 'TEXTAREA' ||
                               alvo.tagName === 'SELECT' || alvo.isContentEditable);

      /* PORTE WINDOWS: era so ev.metaKey (o Command do Mac). Aqui o atalho e
         Ctrl+1/2/3; o metaKey fica por compatibilidade e nao atrapalha. */
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey &&
          (ev.key === '1' || ev.key === '2' || ev.key === '3')) {
        ev.preventDefault();
        trocarAba(ABAS_ORDEM[+ev.key - 1], true);
        return;
      }
      if (ev.key === '/' && !digitando && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
        ev.preventDefault();
        trocarAba('historico');
        $('busca').focus();
        $('busca').select();
        return;
      }
      if (ev.key === 'Escape') {
        /* cascata: um passo atras por vez */
        if (fecharConfirmacaoReiniciar && fecharConfirmacaoReiniciar()) {
          ev.preventDefault();
          return;
        }
        if (fecharCaixaExportar && fecharCaixaExportar()) {
          ev.preventDefault();
          return;
        }
        var campo = $('busca');
        if (campo.value) {
          campo.value = '';
          estado.q = '';
          atualizarChip();
          carregar(true);
          return;
        }
        if (document.activeElement === campo) { campo.blur(); atualizarChip(); return; }
        var aberto = lista.querySelector('.ditado.aberto');
        if (aberto) { fecharCartao(aberto); }
      }
    });
  }

  function medirTopo() {
    var h = $('topo').getBoundingClientRect().height;
    document.documentElement.style.setProperty('--h-topo', Math.round(h) + 'px');
  }

  function arrancar() {
    iniciarSessao();
    if (!sessao) {
      /* sem token nao adianta pedir nada: mostra a caixa na hora em vez de
         disparar meia duzia de fetch que voltariam 403 */
      mostrarBloqueio();
      return;
    }

    /* filtros salvos */
    $('filtro-dia').value = estado.modoDia;
    $('filtro-data').value = estado.data;
    marcarFiltros();
    atualizarChip();

    montarGrupos();
    ligarAbas();
    ligarBusca();
    ligarAtalhos();
    ligarAcoes();
    ligarExportar();
    ligarInsights();
    ligarSentinela();

    trocarAba(estado.aba);

    carregarResumo();
    carregarApps();
    carregar(true);
    buscarSaude();      /* o veredito do cabecalho vale nas duas telas */

    medirTopo();
    window.addEventListener('resize', medirTopo);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { pararTimerSaude(); }
      else { if (estado.aba === 'saude') { buscarSaude(); } ligarTimerSaude(); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', arrancar);
  } else {
    arrancar();
  }
})();
