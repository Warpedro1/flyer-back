# Modelos de e-mail do Supabase Auth (Flyer)

Copie para **Supabase → Authentication → Emails → Templates**. O conector não altera
a configuração de Auth: isso é feito no painel. Os textos são em português do Brasil.

| Modelo no painel | Arquivo | Assunto |
|---|---|---|
| Confirm signup | `confirm_signup.html` | Seu código para ativar a conta no Flyer |
| Magic Link | `magic_link_code.html` | Seu código de acesso ao Flyer |
| Reset Password | `reset_password.html` | Seu código para criar uma senha nova no Flyer |

O cadastro, o login com senha e a troca de senha usam **códigos** de 6 dígitos (`{{ .Token }}`), sem links
(`tests/test_email_templates.py`). Os antivírus de e-mail institucional abrem todos os links
de uma mensagem segundos depois de ela chegar e gastariam um link de uso único (#43):
- **Confirm signup** (decisões 2026-10-14): o código só ativa a conta no navegador que fez
  o cadastro (qualquer aba), que salva o ticket devolvido por `POST /auth/signup`
  (`POST /auth/signup/activate`, `app/api/auth.py`). O modelo não pode ter
  `{{ .ConfirmationURL }}`: só `{{ .Token }}`.
- **Magic Link**: o código do login com senha, pedido pelo backend em `POST /auth/v1/otp`.
- **Reset Password**: o código que a tela `/reset-password` pede junto com a senha nova
  (`verifyOtp` com `type: 'recovery'`).

Configuração que acompanha estes modelos (decisões 2026-10-12):
- Providers → Email: Confirm email ativado; Email OTP length 6; Email OTP expiration 600 s.
- Password: mínimo 8, com letras e números; Leaked password protection ativado (exige o plano Pro).
- URL Configuration: Site URL `https://flyer-inky.vercel.app`; Redirect URLs
  `https://flyer-inky.vercel.app/**` e `http://localhost:5173/**`.
- SMTP próprio (Resend, Brevo…): o SMTP de teste do Supabase só envia para a equipe.
