import utils.net
import utils.clients
import utils.http_session
import utils.transport
import utils.session
from odoo.addons.auth_signup.controllers.main import AuthSignupHome
from odoo.http import request, route

class WhatsAppSignup(AuthSignupHome):

    @route()
    def web_auth_signup(self, *args, **kw):
        response = super(WhatsAppSignup, self).web_auth_signup(*args, **kw)
        
        # If the response is a redirect to /web (successful login)
        if hasattr(response, 'status_code') and response.status_code in (301, 302, 303):
            location = response.headers.get('Location', '')
            if location == '/web' or location.startswith('/web?'):
                # Check if it's a whatsapp signup
                if kw.get('is_whatsapp_signup'):
                    # Redirect to WhatsApp Chats action
                    response.headers['Location'] = '/web#action=whatsapp_web_chats.action_whatsapp_web_chats'
                    
        return response

