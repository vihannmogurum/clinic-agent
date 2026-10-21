/**
 * Clinic Chat Widget - Embeddable Web Chat
 * Usage: <script src="/static/widget.js" data-api-base="https://your-domain.com"></script>
 */

(function () {
    'use strict';

    // Configuration
    const CONFIG = {
        apiBase: '',
        position: 'bottom-right',
        theme: {
            primary: '#2563eb',
            background: '#ffffff',
            text: '#1f2937',
            userBubble: '#2563eb',
            botBubble: '#f3f4f6',
        },
        strings: {
            placeholder: 'Type a message...',
            send: 'Send',
            loading: 'Typing...',
            welcome: 'Hello! How can I help you today?',
            error: 'Something went wrong. Please try again.',
        },
    };

    // Parse configuration from script tag
    function parseConfig() {
        const script = document.currentScript;
        if (script) {
            CONFIG.apiBase = script.getAttribute('data-api-base') || '';
            CONFIG.position = script.getAttribute('data-position') || CONFIG.position;
        }
    }

    // Utility functions
    function createElement(tag, className, innerHTML) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (innerHTML) el.innerHTML = innerHTML;
        return el;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function formatTime(date) {
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    // Widget state
    let isOpen = false;
    let sessionId = localStorage.getItem('clinic_chat_session_id') || generateSessionId();
    let messageHistory = [];

    function generateSessionId() {
        const id = 'session_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
        localStorage.setItem('clinic_chat_session_id', id);
        return id;
    }

    // Create widget elements
    function createWidget() {
        // Main container
        const container = createElement('div', 'clinic-chat-widget');
        container.style.cssText = `
            position: fixed;
            z-index: 9999;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 14px;
            line-height: 1.5;
        `;

        // Launcher bubble
        const launcher = createElement('button', 'clinic-chat-launcher', `
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
            </svg>
        `);
        launcher.style.cssText = `
            width: 60px;
            height: 60px;
            border-radius: 50%;
            background: ${CONFIG.theme.primary};
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
            transition: transform 0.2s, box-shadow 0.2s;
        `;
        launcher.setAttribute('aria-label', 'Open chat');
        launcher.onmouseover = () => {
            launcher.style.transform = 'scale(1.1)';
            launcher.style.boxShadow = '0 6px 20px rgba(0, 0, 0, 0.2)';
        };
        launcher.onmouseout = () => {
            launcher.style.transform = 'scale(1)';
            launcher.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.15)';
        };

        // Chat window
        const window = createElement('div', 'clinic-chat-window');
        window.style.cssText = `
            position: absolute;
            bottom: 80px;
            right: 0;
            width: 360px;
            max-height: 500px;
            background: ${CONFIG.theme.background};
            border-radius: 16px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
            display: none;
            flex-direction: column;
            overflow: hidden;
            border: 1px solid #e5e7eb;
        `;

        // Header
        const header = createElement('div', 'clinic-chat-header', `
            <div style="display: flex; align-items: center; gap: 12px;">
                <div style="width: 40px; height: 40px; border-radius: 50%; background: ${CONFIG.theme.primary}; display: flex; align-items: center; justify-content: center; color: white;">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
                        <polyline points="22 4 12 14.01 9 11.01"></polyline>
                    </svg>
                </div>
                <div>
                    <div style="font-weight: 600; color: ${CONFIG.theme.text};">City Health Clinic</div>
                    <div style="font-size: 12px; color: #10b981;">Online now</div>
                </div>
            </div>
            <button class="clinic-chat-close" style="
                background: none;
                border: none;
                cursor: pointer;
                padding: 8px;
                color: #6b7280;
                border-radius: 8px;
                transition: background 0.2s;
            ">✕</button>
        `);
        header.style.cssText = `
            padding: 16px;
            background: ${CONFIG.theme.background};
            border-bottom: 1px solid #e5e7eb;
            display: flex;
            justify-content: space-between;
            align-items: center;
        `;

        // Messages container
        const messagesContainer = createElement('div', 'clinic-chat-messages');
        messagesContainer.style.cssText = `
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        `;

        // Typing indicator
        const typingIndicator = createElement('div', 'clinic-chat-typing', `
            <div style="display: flex; gap: 4px; padding: 8px 16px;">
                <span style="width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; animation: bounce 1.4s infinite ease-in-out;"></span>
                <span style="width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; animation: bounce 1.4s infinite ease-in-out 0.2s;"></span>
                <span style="width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; animation: bounce 1.4s infinite ease-in-out 0.4s;"></span>
            </div>
        `);
        typingIndicator.style.cssText = `
            display: none;
            padding: 0 16px 8px;
        `;

        // Input area
        const inputArea = createElement('div', 'clinic-chat-input-area');
        inputArea.style.cssText = `
            padding: 12px 16px;
            border-top: 1px solid #e5e7eb;
            background: ${CONFIG.theme.background};
            display: flex;
            gap: 8px;
        `;
        inputArea.innerHTML = `
            <input type="text" class="clinic-chat-input" placeholder="${CONFIG.strings.placeholder}" style="
                flex: 1;
                padding: 12px 16px;
                border: 1px solid #e5e7eb;
                border-radius: 24px;
                font-size: 14px;
                outline: none;
                transition: border-color 0.2s;
            ">
            <button class="clinic-chat-send" style="
                width: 44px;
                height: 44px;
                border-radius: 50%;
                background: ${CONFIG.theme.primary};
                border: none;
                color: white;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: background 0.2s;
            ">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="22" y1="2" x2="11" y2="13"></line>
                    <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                </svg>
            </button>
        `;

        // Add keyframes for typing animation
        const style = document.createElement('style');
        style.textContent = `
            @keyframes bounce {
                0%, 80%, 100% { transform: scale(0); }
                40% { transform: scale(1); }
            }
            .clinic-chat-input:focus { border-color: ${CONFIG.theme.primary} !important; }
            .clinic-chat-send:hover { background: ${shadeColor(CONFIG.theme.primary, -20)} !important; }
            .clinic-chat-close:hover { background: #f3f4f6 !important; }
        `;
        document.head.appendChild(style);

        // Assemble window
        window.appendChild(header);
        window.appendChild(messagesContainer);
        window.appendChild(typingIndicator);
        window.appendChild(inputArea);

        // Position launcher
        if (CONFIG.position === 'bottom-left') {
            container.style.bottom = '24px';
            container.style.left = '24px';
            window.style.right = 'auto';
            window.style.left = '0';
            launcher.style.left = '0';
        } else {
            container.style.bottom = '24px';
            container.style.right = '24px';
            window.style.right = '0';
            launcher.style.right = '0';
        }

        container.appendChild(window);
        container.appendChild(launcher);
        document.body.appendChild(container);

        return {
            container,
            launcher,
            window,
            header,
            messagesContainer,
            typingIndicator,
            inputArea,
            input: container.querySelector('.clinic-chat-input'),
            sendBtn: container.querySelector('.clinic-chat-send'),
            closeBtn: container.querySelector('.clinic-chat-close'),
        };
    }

    function shadeColor(color, percent) {
        const num = parseInt(color.replace('#', ''), 16);
        const amt = Math.round(2.55 * percent);
        const R = (num >> 16) + amt;
        const G = (num >> 8 & 0x00FF) + amt;
        const B = (num & 0x0000FF) + amt;
        return '#' + (0x1000000 + (R < 255 ? R < 1 ? 0 : R : 255) * 0x10000 +
            (G < 255 ? G < 1 ? 0 : G : 255) * 0x100 +
            (B < 255 ? B < 1 ? 0 : B : 255)).toString(16).slice(1);
    }

    // API calls
    async function sendMessage(message) {
        try {
            const response = await fetch(`${CONFIG.apiBase}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: message,
                    session_id: sessionId,
                }),
            });
            const data = await response.json();
            return data;
        } catch (error) {
            console.error('Chat API error:', error);
            return { reply: CONFIG.strings.error, booked: false };
        }
    }

    // UI functions
    function addMessage(text, isUser, isLoading = false) {
        const { messagesContainer } = elements;
        const messageDiv = createElement('div', `clinic-chat-message ${isUser ? 'user' : 'bot'}`);
        messageDiv.style.cssText = `
            display: flex;
            ${isUser ? 'justify-content: flex-end;' : 'justify-content: flex-start;'}
            animation: fadeIn 0.3s ease-out;
        `;

        const bubble = createElement('div', 'clinic-chat-bubble');
        const bgColor = isUser ? CONFIG.theme.userBubble : CONFIG.theme.botBubble;
        const textColor = isUser ? 'white' : CONFIG.theme.text;
        bubble.style.cssText = `
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 18px;
            background: ${bubbleColor};
            color: ${textColor};
            ${isUser ? 'border-bottom-right-radius: 4px;' : 'border-bottom-left-radius: 4px;'}
        `;

        if (isLoading) {
            bubble.innerHTML = `
                <div style="display: flex; gap: 4px;">
                    <span style="width: 8px; height: 8px; border-radius: 50%; background: ${shadeColor(bubbleColor, 30)}; animation: bounce 1.4s infinite ease-in-out;"></span>
                    <span style="width: 8px; height: 8px; border-radius: 50%; background: ${shadeColor(bubbleColor, 30)}; animation: bounce 1.4s infinite ease-in-out 0.2s;"></span>
                    <span style="width: 8px; height: 8px; border-radius: 50%; background: ${shadeColor(bubbleColor, 30)}; animation: bounce 1.4s infinite ease-in-out 0.4s;"></span>
                </div>
            `;
        } else {
            // Convert newlines to <br>
            bubble.innerHTML = escapeHtml(text).replace(/\n/g, '<br>');
        }

        messageDiv.appendChild(bubble);
        messagesContainer.appendChild(messageDiv);
        messagesContainer.scrollTop = messagesContainer.scrollHeight;

        if (isLoading) {
            return bubble;
        }

        // Add timestamp
        const time = createElement('div', 'clinic-chat-time');
        time.style.cssText = `
            font-size: 10px;
            color: #9ca3af;
            margin-top: 4px;
            ${isUser ? 'text-align: right;' : 'text-align: left;'}
            padding: 0 4px;
        `;
        time.textContent = formatTime(new Date());
        messagesContainer.appendChild(time);

        return null;
    }

    function removeLoadingMessage(bubble) {
        const messageDiv = bubble.closest('.clinic-chat-message');
        if (messageDiv) {
            messageDiv.remove();
        }
    }

    function showTyping() {
        elements.typingIndicator.style.display = 'block';
        elements.messagesContainer.scrollTop = elements.messagesContainer.scrollHeight;
    }

    function hideTyping() {
        elements.typingIndicator.style.display = 'none';
    }

    function openChat() {
        isOpen = true;
        elements.window.style.display = 'flex';
        elements.launcher.style.display = 'none';
        elements.input.focus();
    }

    function closeChat() {
        isOpen = false;
        elements.window.style.display = 'none';
        elements.launcher.style.display = 'flex';
    }

    function toggleChat() {
        if (isOpen) closeChat();
        else openChat();
    }

    async function handleSend() {
        const text = elements.input.value.trim();
        if (!text) return;

        elements.input.value = '';
        addMessage(text, true);

        const loadingBubble = addMessage('', false, true);
        showTyping();

        const response = await sendMessage(text);
        hideTyping();
        removeLoadingMessage(loadingBubble);
        addMessage(response.reply, false);

        if (response.booked) {
            // Show success state - could add confetti or special styling
            setTimeout(() => {
                // Could add celebration effect here
            }, 500);
        }
    }

    // Initialize
    parseConfig();
    const elements = createWidget();

    // Event listeners
    elements.launcher.addEventListener('click', toggleChat);
    elements.closeBtn.addEventListener('click', closeChat);
    elements.sendBtn.addEventListener('click', handleSend);
    elements.input.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') handleSend();
    });

    // Add welcome message after a short delay
    setTimeout(() => {
        addMessage(CONFIG.strings.welcome, false);
    }, 500);

    // Expose API for manual control
    window.ClinicChat = {
        open: openChat,
        close: closeChat,
        toggle: toggleChat,
        sendMessage: (msg) => {
            elements.input.value = msg;
            handleSend();
        },
        setSessionId: (id) => {
            sessionId = id;
            localStorage.setItem('clinic_chat_session_id', id);
        },
        getSessionId: () => sessionId,
    };
})();