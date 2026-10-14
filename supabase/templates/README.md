# Modelos de e-mail do Supabase Auth (Flyer)

Copiar para **Supabase → Authentication → Emails → Templates**. O conector não altera
a configuração de Auth: isto é feito no painel.

| Modelo no painel | Ficheiro | Assunto |
|---|---|---|
| Confirm signup | `confirm_signup.html` | O teu código para ativar a conta no Flyer |
| Magic Link | `magic_link_code.html` | O teu código de acesso ao Flyer |
| Reset Password | `reset_password.html` | Criar uma senha nova no Flyer |

O cadastro e o login com senha usam **códigos** de 6 dígitos (`{{ .Token }}`), sem links:
- **Confirm signup** (decisões 2026-10-14): o código só ativa a conta no navegador que fez
  o cadastro (qualquer separador), que guarda o bilhete devolvido por `POST /auth/signup`
  (`POST /auth/signup/activate`, `app/api/auth.py`). O modelo não pode ter
  `{{ .ConfirmationURL }}`: só `{{ .Token }}`.
- **Magic Link**: o código do login com senha, pedido pelo backend em `POST /auth/v1/otp`.

Configuração que acompanha estes modelos (decisões 2026-10-12):
- Providers → Email: Confirm email ligado; Email OTP length 6; Email OTP expiration 600 s.
- Password: mínimo 8, com letras e números; Leaked password protection ligado.
- URL Configuration: Site URL `https://flyer-inky.vercel.app`; Redirect URLs
  `https://flyer-inky.vercel.app/**` e `http://localhost:5173/**`.
- SMTP próprio (Resend, Brevo…): o SMTP de teste do Supabase só envia para a equipa.
