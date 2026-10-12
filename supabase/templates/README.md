# Modelos de e-mail do Supabase Auth (Flyer)

Copiar para **Supabase → Authentication → Emails → Templates**. O conector não altera
a configuração de Auth: isto é feito no painel.

| Modelo no painel | Ficheiro | Assunto |
|---|---|---|
| Confirm signup | `confirm_signup.html` | Confirma o teu e-mail no Flyer |
| Magic Link | `magic_link_code.html` | O teu código de acesso ao Flyer |
| Reset Password | `reset_password.html` | Criar uma senha nova no Flyer |

O login com senha usa o modelo **Magic Link** para enviar o **código** de 6 dígitos
(`{{ .Token }}`): o backend pede-o em `POST /auth/v1/otp` (`app/api/auth.py`).

Configuração que acompanha estes modelos (decisões 2026-10-12):
- Providers → Email: Confirm email ligado; Email OTP length 6; Email OTP expiration 600 s.
- Password: mínimo 8, com letras e números; Leaked password protection ligado.
- URL Configuration: Site URL `https://flyer-inky.vercel.app`; Redirect URLs
  `https://flyer-inky.vercel.app/**` e `http://localhost:5173/**`.
- SMTP próprio (Resend, Brevo…): o SMTP de teste do Supabase só envia para a equipa.
