from odoo import models, fields, api
import requests
import logging
from datetime import datetime
from pytz import timezone

_logger = logging.getLogger(__name__)

class WhatsAppSaaSTenant(models.Model):
    _name = 'whatsapp.saas.tenant'
    _description = 'WhatsApp SaaS Tenant Schedule'

    account_id = fields.Many2one('whatsapp.account', string="WhatsApp Account", required=True)
    tenant_id = fields.Char(string="Tenant ID (SaaS)")
    tenant_name = fields.Char(string="Tenant Name")
    tenant_phone = fields.Char(string="Tenant Phone")
    
    scheduled_time = fields.Float(string="Scheduled Send Time", default=17.5, help="Time to send daily sales report (e.g. 17.5 = 5:30 PM)")
    last_sales_sent_date = fields.Date(string="Last Sales Sent Date")
    welcome_message_sent = fields.Boolean(string="Welcome Message Sent", default=False)
    
    tenant_expiration_date = fields.Date(string="Expiration Date")
    last_expiration_sent_days = fields.Integer(string="Last Expiration Sent (Days Left)", default=-1)

    @api.model
    def _cron_sync_and_send_saas_data(self):
        accounts = self.env['whatsapp.account'].search([('saas_integration_active', '=', True)])
        if not accounts:
            return

        for account in accounts:
            self._sync_tenants_from_saas(account)
            self._process_daily_sales(account)
            self._process_expirations(account)

    def _sync_tenants_from_saas(self, account):
        # Local SaaS setup check
        has_local_saas = 'havanoposdesk.tenant' in self.env
        
        tenants_data = []
        if has_local_saas:
            # Sync directly from local HavanoPOS module
            local_tenants = self.env['havanoposdesk.tenant'].sudo().search([])
            for t in local_tenants:
                phone = getattr(t, 'phone', '')
                if not phone and hasattr(t, 'admin_id') and getattr(t, 'admin_id'):
                    phone = t.admin_id.phone
                
                if not phone:
                    # Look for an admin user belonging to this tenant
                    admin_user = self.env['res.users'].sudo().search([
                        ('tenant_id', '=', t.id),
                        ('havano_role', 'in', ['admin', 'super_admin'])
                    ], limit=1)
                    if not admin_user:
                        admin_user = self.env['res.users'].sudo().search([('tenant_id', '=', t.id)], limit=1)
                    
                    if admin_user:
                        phone = admin_user.phone or getattr(admin_user.partner_id, 'phone', '') or getattr(admin_user.partner_id, 'mobile', '')
                
                exp_date = getattr(t, 'expiration_date', False) or getattr(t, 'subscription_end_date', False) or getattr(t, 'subscription_end', False)
                
                tenants_data.append({
                    'id': str(t.id),
                    'name': t.name,
                    'phone': phone,
                    'expiration_date': exp_date,
                    'create_date': getattr(t, 'create_date', False)
                })
        elif account.saas_app_url:
            try:
                headers = {}
                if account.saas_username and account.saas_password:
                    headers['Authorization'] = f'token {account.saas_username}:{account.saas_password}'
                
                response = requests.get(f"{account.saas_app_url.rstrip('/')}/api/method/saas_api.www.api.get_users", headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    message_dict = data.get('message', {})
                    users = message_dict.get('data', []) if isinstance(message_dict, dict) else []
                    for t in users:
                        phone = t.get('phone_number') or t.get('mobile_no') or t.get('phone')
                        t_id = t.get('tenant_id') or t.get('id')
                        tenants_data.append({
                            'id': str(t_id),
                            'name': t.get('full_name') or t.get('name') or t.get('username'),
                            'phone': phone,
                            'expiration_date': t.get('expiration_date') or t.get('subscription_end_date') or False
                        })
            except Exception as e:
                _logger.error(f"Error fetching remote tenants from SaaS API: {e}")
                
        existing_tenant_ids = set(self.search([('account_id', '=', account.id)]).mapped('tenant_id'))
        new_tenants_data = [t for t in tenants_data if t['id'] not in existing_tenant_ids]
        is_bulk_import = len(new_tenants_data) > 3
        
        for t_data in tenants_data:
            if not t_data.get('phone'):
                continue
                
            if t_data['id'] not in existing_tenant_ids:
                new_tenant = self.create({
                    'account_id': account.id,
                    'tenant_id': t_data['id'],
                    'tenant_name': t_data['name'],
                    'tenant_phone': t_data['phone'],
                    'tenant_expiration_date': t_data.get('expiration_date'),
                })
                
                should_send = not is_bulk_import
                if t_data.get('create_date'):
                    delta = fields.Datetime.now() - t_data['create_date']
                    if delta.days > 1:
                        should_send = False
                
                # Send welcome message upon new tenant discovery
                if should_send and account.saas_welcome_template_id:
                    self._send_whatsapp_message(
                        account, 
                        t_data['phone'], 
                        account.saas_welcome_template_id, 
                        [t_data['name']]
                    )
                new_tenant.welcome_message_sent = True
            else:
                existing = self.search([('account_id', '=', account.id), ('tenant_id', '=', t_data['id'])], limit=1)
                existing.write({
                    'tenant_phone': t_data['phone'],
                    'tenant_name': t_data['name'],
                    'tenant_expiration_date': t_data.get('expiration_date'),
                })

    def _process_daily_sales(self, account):
        tz = timezone(self.env.user.tz or 'UTC')
        now = datetime.now(tz)
        current_time_float = now.hour + now.minute / 60.0
        current_date = now.date()

        # Find tenants whose scheduled time is reached and haven't received it today
        tenants_to_send = self.search([
            ('account_id', '=', account.id),
            ('scheduled_time', '<=', current_time_float),
            '|', ('last_sales_sent_date', '!=', current_date), ('last_sales_sent_date', '=', False)
        ])

        has_local_saas = 'havanoposdesk.tenant' in self.env

        for tenant in tenants_to_send:
            store_lines = []
            if has_local_saas:
                try:
                    stores = self.env['havanoposdesk.store'].sudo().search([('tenant_id', '=', int(tenant.tenant_id))])

                    for store in stores:
                        # Query today's POS orders for this store directly from local models
                        today_start = datetime.now(timezone(self.env.user.tz or 'UTC')).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        orders = self.env['pos.order'].sudo().search([
                            ('store_id', '=', store.id),
                            ('date_order', '>=', today_start.strftime('%Y-%m-%d %H:%M:%S')),
                            ('state', 'in', ['done', 'invoiced']),
                        ])
                        total = sum(orders.mapped('amount_total'))
                        num_orders = len(orders)
                        currency = store.currency_id.symbol if hasattr(store, 'currency_id') and store.currency_id else '$'
                        store_lines.append(
                            f"🏪 *{store.name}*\n"
                            f"   Sales: {currency}{total:,.2f}  |  Orders: {num_orders}"
                        )
                except Exception as e:
                    _logger.error(f"Error fetching local sales for tenant {tenant.tenant_name}: {e}")
            elif account.saas_app_url:
                try:
                    headers = {}
                    if account.saas_username and account.saas_password:
                        headers['Authorization'] = f'token {account.saas_username}:{account.saas_password}'
                        
                    response = requests.get(
                        f"{account.saas_app_url.rstrip('/')}/api/reports/daily-sales",
                        params={'tenant_id': tenant.tenant_id},
                        headers=headers,
                        timeout=10
                    )
                    if response.status_code == 200:
                        data = response.json()
                        records = data.get('data', [])
                        if records:
                            total = sum(r.get('total_sales', 0) for r in records)
                            num_orders = sum(r.get('total_qty', 0) for r in records)
                            currency = '$'
                            store_name = 'Main Branch'
                            avg_order = (total / num_orders) if num_orders > 0 else 0.0
                            
                            formatted_date = current_date.strftime("%d %b %Y")
                            
                            variables = [
                                tenant.tenant_name,
                                formatted_date,
                                f"{currency}{total:,.2f}",
                                store_name,
                                str(int(num_orders)),
                                f"{currency}{avg_order:,.2f}"
                            ]
                            
                            if account.saas_daily_sales_template_id:
                                self._send_whatsapp_message(
                                    account,
                                    tenant.tenant_phone,
                                    account.saas_daily_sales_template_id,
                                    variables
                                )
                except Exception as e:
                    _logger.error(f"Error fetching remote sales for tenant {tenant.tenant_name}: {e}")

            tenant.last_sales_sent_date = current_date


    def _send_whatsapp_message(self, account, phone, template, variables):
        try:
            free_text_json = {}
            for i, var in enumerate(variables):
                free_text_json[f'free_text_{i + 1}'] = str(var)

            local_partner = self.env['res.partner'].search(
                ['|', ('phone', '=', phone), ('phone', '=', phone.lstrip('+'))], limit=1
            )
            if not local_partner:
                local_partner = self.env.user.partner_id
                
            target_model = template.model_id.model or 'res.partner'
            if target_model == 'res.partner' and local_partner:
                target_record = local_partner
            else:
                target_record = self.env[target_model].sudo().search([], limit=1)
                if not target_record:
                    target_record = local_partner # Fallback
            
            # Render the template body to show the exact message in the Odoo UI
            rendered_body = template.body or ''
            for i, var in enumerate(variables):
                rendered_body = rendered_body.replace(f'{{{{{i + 1}}}}}', str(var))
            
            # Formatting for Odoo UI (newlines to HTML breaks if needed, though message_post handles basic text)
            # We'll just prefix it slightly to indicate it's a template
            ui_body = f"<strong>WhatsApp Template Sent:</strong><br/><br/>{rendered_body.replace('\n', '<br/>')}"
            
            # Post message to the exact model required by the template to avoid Odoo template validation error
            mail_msg = target_record.sudo().message_post(
                body=ui_body,
                message_type='comment',
                subtype_xmlid='mail.mt_note',
                author_id=self.env.user.partner_id.id,
            )
            
            msg_vals = {
                'wa_account_id': account.id,
                'mobile_number': phone,
                'wa_template_id': template.id,
                'free_text_json': free_text_json,
                'mail_message_id': mail_msg.id,
                'body': rendered_body,
                'state': 'outgoing',
                'message_type': 'outbound',
            }
            # Variables are passed via free_text_json only (free_text_N fields don't exist in Odoo 19)

            wa_msg = self.env['whatsapp.message'].create(msg_vals)
            wa_msg._send(force_send_by_cron=False)
            
            _logger.info(f"SaaS notification sent successfully to {phone}")
        except Exception as e:
            import traceback
            _logger.error(f"Failed to send WA SaaS message to {phone}: {e}")
            print(f"ERROR sending to {phone}: {e}")
            print(traceback.format_exc())

    def _process_expirations(self, account):
        if not account.saas_expiration_template_id or not account.saas_expiration_days:
            return
            
        tz = timezone(self.env.user.tz or 'UTC')
        now = datetime.now(tz)
        current_date = now.date()
        current_time_float = now.hour + now.minute / 60.0
        
        try:
            warning_days = [int(d.strip()) for d in account.saas_expiration_days.split(',') if d.strip().isdigit()]
        except Exception:
            return
            
        tenants_to_check = self.search([
            ('account_id', '=', account.id),
            ('tenant_expiration_date', '!=', False),
            ('scheduled_time', '<=', current_time_float),
        ])
        
        for tenant in tenants_to_check:
            days_left = (tenant.tenant_expiration_date - current_date).days
            if days_left in warning_days and tenant.last_expiration_sent_days != days_left:
                self._send_whatsapp_message(
                    account,
                    tenant.tenant_phone,
                    account.saas_expiration_template_id,
                    [tenant.tenant_name, str(days_left)]
                )
                tenant.last_expiration_sent_days = days_left
