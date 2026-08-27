(() => {
  'use strict';

  const VERSION = 2;
  const AAD_TEXT = 'Expensetics device unlock v1';

  const randomBytes = length => crypto.getRandomValues(new Uint8Array(length));

  const toBase64Url = value => {
    const bytes = new Uint8Array(value);
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '');
  };

  const fromBase64Url = value => {
    const padded = value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - value.length % 4) % 4);
    return Uint8Array.from(atob(padded), character => character.charCodeAt(0));
  };

  const classifyError = error => {
    if (error?.name === 'NotAllowedError' || error?.name === 'AbortError') return 'cancelled';
    if (error?.name === 'OperationError') return 'decrypt_failed';
    if (error?.name === 'SecurityError') return 'invalid_origin';
    if (error?.name === 'NotSupportedError') return 'unsupported';
    return 'unknown';
  };

  const supported = async () => {
    if (!window.isSecureContext) return 'not_secure';
    // WebAuthn permits plain HTTP only for the localhost domain. Loopback IP
    // literals are secure contexts for other APIs but are not valid RP IDs.
    if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
      return 'invalid_origin';
    }
    if (
      !window.PublicKeyCredential || !navigator.credentials || !window.crypto?.subtle ||
      !window.TextEncoder || !window.TextDecoder
    ) return 'unsupported';
    const available = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
    if (!available) return 'unavailable';
    if (PublicKeyCredential.getClientCapabilities) {
      const capabilities = await PublicKeyCredential.getClientCapabilities();
      if (capabilities['extension:prf'] === false) return 'prf_unsupported';
    }
    return null;
  };

  const evaluate = async (credentialId, salt, transports, rpId) => {
    const credential = await navigator.credentials.get({
      publicKey: {
        challenge: randomBytes(32),
        rpId,
        allowCredentials: [{type: 'public-key', id: credentialId, transports}],
        userVerification: 'required',
        timeout: 60000,
        extensions: {prf: {eval: {first: salt}}},
      },
    });
    return credential.getClientExtensionResults()?.prf?.results?.first || null;
  };

  const encrypt = async (password, prf, iv) => {
    const key = await crypto.subtle.importKey('raw', prf, 'AES-GCM', false, ['encrypt']);
    const encoder = new TextEncoder();
    return crypto.subtle.encrypt(
      {name: 'AES-GCM', iv, additionalData: encoder.encode(AAD_TEXT)},
      key,
      encoder.encode(password),
    );
  };

  const decrypt = async (record, prf) => {
    const key = await crypto.subtle.importKey('raw', prf, 'AES-GCM', false, ['decrypt']);
    const plaintext = await crypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: fromBase64Url(record.iv),
        additionalData: new TextEncoder().encode(AAD_TEXT),
      },
      key,
      fromBase64Url(record.ciphertext),
    );
    return new TextDecoder('utf-8', {fatal: true}).decode(plaintext);
  };

  window.expenseticsDeviceUnlock = {
    async availability() {
      try {
        const problem = await supported();
        return problem ? {ok: false, error: problem} : {ok: true};
      } catch (error) {
        return {ok: false, error: classifyError(error)};
      }
    },

    async enroll(password) {
      try {
        const problem = await supported();
        if (problem) return {ok: false, error: problem};

        const salt = randomBytes(32);
        const credential = await navigator.credentials.create({
          publicKey: {
            challenge: randomBytes(32),
            rp: {name: 'Expensetics'},
            user: {
              id: randomBytes(32),
              name: 'local-vault',
              displayName: 'Expensetics local vault',
            },
            pubKeyCredParams: [{type: 'public-key', alg: -7}, {type: 'public-key', alg: -257}],
            authenticatorSelection: {
              authenticatorAttachment: 'platform',
              residentKey: 'discouraged',
              userVerification: 'required',
            },
            hints: ['client-device'],
            timeout: 60000,
            attestation: 'none',
            extensions: {prf: {eval: {first: salt}}},
          },
        });

        const extension = credential.getClientExtensionResults()?.prf;
        if (extension?.enabled !== true) return {ok: false, error: 'prf_unsupported'};
        const reportedTransports = credential.response.getTransports?.() || [];
        const transports = reportedTransports.length ? reportedTransports : ['internal'];
        const prf = extension.results?.first || await evaluate(
          credential.rawId, salt, transports, location.hostname,
        );
        if (!prf) return {ok: false, error: 'prf_unsupported'};

        const iv = randomBytes(12);
        const ciphertext = await encrypt(password, prf, iv);
        return {
          ok: true,
          record: {
            version: VERSION,
            credential_id: toBase64Url(credential.rawId),
            salt: toBase64Url(salt),
            iv: toBase64Url(iv),
            ciphertext: toBase64Url(ciphertext),
            rp_id: location.hostname,
            transports,
          },
        };
      } catch (error) {
        return {ok: false, error: classifyError(error)};
      } finally {
        password = '';
      }
    },

    async unlock(record) {
      try {
        const problem = await supported();
        if (problem) return {ok: false, error: problem};
        if (record.rp_id !== location.hostname) return {ok: false, error: 'origin_changed'};
        const credentialId = fromBase64Url(record.credential_id);
        const prf = await evaluate(
          credentialId, fromBase64Url(record.salt), record.transports, record.rp_id,
        );
        if (!prf) return {ok: false, error: 'prf_unsupported'};
        return {ok: true, password: await decrypt(record, prf)};
      } catch (error) {
        return {ok: false, error: classifyError(error)};
      }
    },

    async rewrap(record, password) {
      try {
        const problem = await supported();
        if (problem) return {ok: false, error: problem};
        if (record.rp_id !== location.hostname) return {ok: false, error: 'origin_changed'};
        const credentialId = fromBase64Url(record.credential_id);
        const prf = await evaluate(
          credentialId, fromBase64Url(record.salt), record.transports, record.rp_id,
        );
        if (!prf) return {ok: false, error: 'prf_unsupported'};
        const iv = randomBytes(12);
        const ciphertext = await encrypt(password, prf, iv);
        return {
          ok: true,
          record: {...record, iv: toBase64Url(iv), ciphertext: toBase64Url(ciphertext)},
        };
      } catch (error) {
        return {ok: false, error: classifyError(error)};
      } finally {
        password = '';
      }
    },
  };
})();
