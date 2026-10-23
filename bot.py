await asyncio.to_thread(init_store)
    await seed_cats()
    await start_health_server()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    spam = AntiSpam()
    dp.message.outer_middleware(spam)              # anti-spam first
    dp.callback_query.outer_middleware(spam)
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(menu)      # bottom buttons first (they cancel any FSM state)
    dp.include_router(admin)
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Open main menu"),
                               BotCommand(command="cancel", description="Cancel current action")])
    log.info("Bot started as @%s", (await bot.get_me()).username)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass