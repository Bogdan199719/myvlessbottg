    @flask_app.route('/users/issue-key/<int:user_id>', methods=['POST'])
    @login_required
    def issue_key_route(user_id):
        try:
            plan_id = request.form.get('plan_id')
            if not plan_id:
                flash("Ошибка: не выбран тариф.", "danger")
                return redirect(url_for('users_page'))

            plan = get_plan_by_id(int(plan_id))
            if not plan:
                flash("Ошибка: Тариф не найден.", "danger")
                return redirect(url_for('users_page'))

            user = get_user(user_id)
            if not user:
                 flash("Ошибка: Пользователь не найден.", "danger")
                 return redirect(url_for('users_page'))

            month_qty = plan['months']
            # Convert months to days (approx). Same logic as bot
            days_to_add = month_qty * 30 
            
            # Logic similar to process_successful_payment
            key_number = get_next_key_number(user_id)
            
            issued_count = 0
            
            if plan['host_name'] == 'ALL':
                # Global Plan
                hosts = get_all_hosts(only_enabled=True)
                for h in hosts:
                     try:
                        email = f"user{user_id}-key{key_number}-{h['host_name'].replace(' ', '').lower()}"
                        
                        result = asyncio.run(xui_api.create_or_update_key_on_host(
                            host_name=h['host_name'],
                            email=email,
                            days_to_add=days_to_add,
                            telegram_id=str(user_id)
                        ))
                        
                        if result:
                            add_new_key(
                                user_id=user_id,
                                host_name=h['host_name'],
                                xui_client_uuid=result['client_uuid'],
                                key_email=email,
                                expiry_timestamp_ms=result['expiry_timestamp_ms'],
                                connection_string=result['connection_string'],
                                plan_id=plan['plan_id']
                            )
                            issued_count += 1
                     except Exception as e_h:
                         logger.error(f"Failed to issue manual key on host {h['host_name']}: {e_h}")
                
                msg = f"Глобальная подписка успешно выдана! ({issued_count} ключей создано)"
            
            else:
                # Single host
                try:
                    host_name = plan['host_name']
                    email = f"user{user_id}-key{key_number}-{host_name.replace(' ', '').lower()}"
                    
                    result = asyncio.run(xui_api.create_or_update_key_on_host(
                        host_name=host_name,
                        email=email,
                        days_to_add=days_to_add,
                        telegram_id=str(user_id)
                    ))
                    
                    if result:
                        add_new_key(
                            user_id=user_id,
                            host_name=host_name,
                            xui_client_uuid=result['client_uuid'],
                            key_email=email,
                            expiry_timestamp_ms=result['expiry_timestamp_ms'],
                            connection_string=result['connection_string'],
                            plan_id=plan['plan_id']
                        )
                        issued_count += 1
                        msg = f"Подписка на сервер {host_name} успешно выдана!"
                    else:
                        flash("Не удалось создать ключ на сервере XUI.", "danger")
                        return redirect(url_for('users_page'))
                        
                except Exception as e_s:
                     logger.error(f"Failed to issue manual key: {e_s}")
                     flash(f"Ошибка при выдаче: {e_s}", "danger")
                     return redirect(url_for('users_page'))

            # Update user stats (optional - assume free issue doesnt count to spent, or update if needed)
            # For now, we wont add to 'total_spent' since it's manual issue (likely replacement/gift)
            update_user_stats(user_id, 0, month_qty) 
            
            # Notify User
            bot = _bot_controller.get_bot_instance()
            if bot:
                loop = current_app.config.get('EVENT_LOOP')
                verdict_text = f"Администратор выдал вам подписку: <b>{plan['plan_name']}</b>\nСрок: {month_qty} мес."
                if loop and loop.is_running():
                     asyncio.run_coroutine_threadsafe(
                        bot.send_message(user_id, f"🎁 <b>Вам выдана подписка!</b>\n\n{verdict_text}", parse_mode='HTML'),
                        loop
                    )

            flash(msg, "success")
            
        except Exception as e:
            logger.error(f"Error issuing key manually: {e}", exc_info=True)
            flash(f"Ошибка при выдаче подписки: {e}", "danger")

        return redirect(url_for('users_page'))
